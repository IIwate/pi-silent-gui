/** Python helper launch, protocol parsing, and 1 MiB bounded output. */
import { spawn } from "node:child_process";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath } from "node:url";

export type RunResult = {
	ok: boolean;
	data: Record<string, unknown>;
	raw: string;
	exitCode: number | null;
	stdoutTruncated: boolean;
	stderrTruncated: boolean;
};

type OutputTail = {
	chunks: Buffer[];
	length: number;
	truncated: boolean;
};

const EXT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const PY_SCRIPT = path.join(EXT_DIR, "backend", "silent_gui.py");
export const PYTHON_PROTOCOL_PREFIX = "PI_SILENT_GUI_FRAME";
export const PYTHON_PROTOCOL_VERSION = "1";
const MAX_PYTHON_OUTPUT_BYTES = 1024 * 1024;
const MAX_PYTHON_INPUT_BYTES = 1024 * 1024;
const ABORT_CLOSE_GRACE_MS = 500;
const SPAWN_ABORT_GRACE_MS = 11_000;

function appendOutput(tail: OutputTail, chunk: Buffer) {
	tail.chunks.push(chunk);
	tail.length += chunk.length;
	while (tail.length > MAX_PYTHON_OUTPUT_BYTES) {
		const excess = tail.length - MAX_PYTHON_OUTPUT_BYTES;
		const first = tail.chunks[0];
		if (first.length <= excess) {
			tail.chunks.shift();
			tail.length -= first.length;
		} else {
			tail.chunks[0] = Buffer.from(first.subarray(excess));
			tail.length -= excess;
		}
		tail.truncated = true;
	}
}

function outputBuffer(tail: OutputTail): Buffer {
	return Buffer.concat(tail.chunks, tail.length);
}

function trimUtf8Tail(value: string, maxBytes: number): string {
	const encoded = Buffer.from(value, "utf8");
	if (encoded.length <= maxBytes) return value;
	let start = encoded.length - maxBytes;
	while (start < encoded.length && (encoded[start] & 0xc0) === 0x80) start++;
	return encoded.subarray(start).toString("utf8");
}

function outputText(tail: OutputTail): string {
	const decoder = new StringDecoder("utf8");
	return trimUtf8Tail(decoder.write(outputBuffer(tail)) + decoder.end(), MAX_PYTHON_OUTPUT_BYTES);
}

export function isWindows(): boolean {
	return process.platform === "win32";
}

function findPython(): string {
	return process.env.PI_SILENT_GUI_PYTHON || (process.platform === "win32" ? "python" : "python3");
}

export function runPython(
	command: string,
	params: Record<string, unknown>,
	signal?: AbortSignal,
	killOnAbort = true,
): Promise<RunResult> {
	const payload = JSON.stringify(params);
	if (Buffer.byteLength(payload, "utf8") > MAX_PYTHON_INPUT_BYTES) {
		return Promise.resolve({
			ok: false,
			data: {
				ok: false,
				error: "request payload exceeds size limit",
				command,
				stdoutTruncated: false,
				stderrTruncated: false,
			},
			raw: "",
			exitCode: null,
			stdoutTruncated: false,
			stderrTruncated: false,
		});
	}
	return new Promise((resolve) => {
		const py = findPython();
		const stdout: OutputTail = { chunks: [], length: 0, truncated: false };
		const stderr: OutputTail = { chunks: [], length: 0, truncated: false };
		let child: ReturnType<typeof spawn>;
		try {
			child = spawn(py, [PY_SCRIPT, command, "--stdin-json"], {
				windowsHide: true,
				stdio: ["pipe", "pipe", "pipe"],
				env: {
					...process.env,
					PYTHONUTF8: "1",
					PYTHONIOENCODING: "utf-8",
				},
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			resolve({
				ok: false,
				data: {
					ok: false,
					error: `failed to start python (${py}): ${message}`,
					command,
					stdoutTruncated: false,
					stderrTruncated: false,
				},
				raw: "",
				exitCode: null,
				stdoutTruncated: false,
				stderrTruncated: false,
			});
			return;
		}

		const stdoutDecoder = new StringDecoder("utf8");
		let stdoutDecoderEnded = false;
		let stdoutLineBuffer = "";
		let stdoutDiscardingLine = false;
		let protocolLine: string | undefined;
		let protocolData: Record<string, unknown> | undefined;
		let protocolError: string | undefined;
		let protocolFrames = 0;
		const inspectProtocolLine = (line: string) => {
			if (!line.startsWith(`${PYTHON_PROTOCOL_PREFIX}\t`)) return;
			if (Buffer.byteLength(line, "utf8") > MAX_PYTHON_OUTPUT_BYTES) {
				protocolError ??= "frame_too_large";
				return;
			}
			protocolFrames++;
			if (protocolFrames > 1) {
				protocolError = "duplicate_frame";
				return;
			}
			const parts = line.split("\t");
			if (parts.length !== 4) {
				protocolError = "invalid_frame";
				return;
			}
			const [, version, frameCommand, json] = parts;
			if (version !== PYTHON_PROTOCOL_VERSION) {
				protocolError = "version_mismatch";
				return;
			}
			if (frameCommand !== command) {
				protocolError = "command_mismatch";
				return;
			}
			try {
				const parsed: unknown = JSON.parse(json);
				if (
					!parsed ||
					typeof parsed !== "object" ||
					Array.isArray(parsed) ||
					typeof (parsed as Record<string, unknown>).ok !== "boolean"
				) {
					protocolError = "invalid_payload";
					return;
				}
				protocolLine = line;
				protocolData = parsed as Record<string, unknown>;
			} catch {
				protocolError = "invalid_payload";
			}
		};
		const inspectProtocolText = (text: string, flush = false) => {
			if (stdoutDiscardingLine) {
				const newline = text.indexOf("\n");
				if (newline === -1) return;
				stdoutDiscardingLine = false;
				text = text.slice(newline + 1);
			}
			stdoutLineBuffer += text;
			let newline = stdoutLineBuffer.indexOf("\n");
			while (newline !== -1) {
				inspectProtocolLine(stdoutLineBuffer.slice(0, newline).replace(/\r$/, ""));
				stdoutLineBuffer = stdoutLineBuffer.slice(newline + 1);
				newline = stdoutLineBuffer.indexOf("\n");
			}
			if (flush) {
				if (stdoutLineBuffer) {
					if (Buffer.byteLength(stdoutLineBuffer, "utf8") <= MAX_PYTHON_OUTPUT_BYTES) {
						inspectProtocolLine(stdoutLineBuffer);
					} else if (stdoutLineBuffer.startsWith(`${PYTHON_PROTOCOL_PREFIX}\t`)) {
						protocolError ??= "frame_too_large";
					}
				}
				stdoutLineBuffer = "";
				stdoutDiscardingLine = false;
			} else if (Buffer.byteLength(stdoutLineBuffer, "utf8") > MAX_PYTHON_OUTPUT_BYTES) {
				if (stdoutLineBuffer.startsWith(`${PYTHON_PROTOCOL_PREFIX}\t`)) {
					protocolError ??= "frame_too_large";
				}
				stdoutLineBuffer = "";
				stdoutDiscardingLine = true;
			} else {
				stdoutLineBuffer = trimUtf8Tail(stdoutLineBuffer, MAX_PYTHON_OUTPUT_BYTES);
			}
		};
		const endProtocolInput = () => {
			if (stdoutDecoderEnded) return;
			stdoutDecoderEnded = true;
			inspectProtocolText(stdoutDecoder.end(), true);
		};
		let settled = false;
		let spawned = false;
		let aborted = false;
		let terminationError: string | undefined;
		let abortTimer: ReturnType<typeof setTimeout> | undefined;
		const cleanup = () => {
			signal?.removeEventListener("abort", onAbort);
			if (abortTimer) clearTimeout(abortTimer);
		};
		const outputState = () => ({
			stdoutTruncated: stdout.truncated,
			stderrTruncated: stderr.truncated,
		});
		const finishAbort = () => {
			if (settled) return;
			settled = true;
			cleanup();
			endProtocolInput();
			const out = outputText(stdout);
			const err = outputText(stderr);
			const state = outputState();
			resolve({
				ok: false,
				data: {
					ok: false,
					error: `python ${command} aborted`,
					command,
					protocol_error: "aborted",
					abort_reason:
						signal?.reason instanceof Error ? signal.reason.message : String(signal?.reason ?? "aborted"),
					...(terminationError ? { termination_error: terminationError } : {}),
					stdout: out.trim(),
					...state,
				},
				raw: out + err,
				exitCode: null,
				...state,
			});
		};
		const onAbort = () => {
			if (aborted || settled) return;
			aborted = true;
			if (killOnAbort) {
				try {
					if (!child.kill()) terminationError = "child.kill returned false";
				} catch (error) {
					terminationError = error instanceof Error ? error.message : String(error);
				}
			}
			// A process can ignore termination or lose its close event. The caller still gets
			// a finite answer; the pipe may wander on briefly, but it no longer owns this promise.
			abortTimer = setTimeout(finishAbort, killOnAbort ? ABORT_CLOSE_GRACE_MS : SPAWN_ABORT_GRACE_MS);
			abortTimer.unref?.();
		};
		const finish = (code: number | null) => {
			if (settled) return;
			if (aborted && killOnAbort) {
				finishAbort();
				return;
			}
			settled = true;
			cleanup();
			endProtocolInput();
			const out = outputText(stdout);
			const err = outputText(stderr);
			const state = outputState();
			if (protocolError) {
				resolve({
					ok: false,
					data: {
						ok: false,
						error: `invalid python ${command} protocol response`,
						command,
						protocol_error: protocolError,
						stdout: out.trim(),
						...state,
					},
					raw: protocolLine ?? out,
					exitCode: code,
					...state,
				});
				return;
			}
			if (!protocolLine || !protocolData) {
				resolve({
					ok: false,
					data: {
						ok: false,
						error: err.trim() || `python exited ${code} without a protocol response`,
						command,
						stdout: out.trim(),
						...state,
					},
					raw: out + err,
					exitCode: code,
					...state,
				});
				return;
			}
			const data = { ...protocolData, ...state };
			const exitOk = code === 0;
			const responseOk = data.ok === true;
			if (exitOk !== responseOk) {
				resolve({
					ok: false,
					data: {
						ok: false,
						error: `backend exit status and ${command} response disagree`,
						command,
						exit_code: code,
						session_id: data.session_id,
						protocol_error: "exit_status_mismatch",
						backend: data,
						...state,
					},
					raw: protocolLine,
					exitCode: code,
					...state,
				});
				return;
			}
			resolve({
				ok: responseOk,
				data,
				raw: protocolLine,
				exitCode: code,
				...state,
			});
		};

		child.once("spawn", () => {
			spawned = true;
		});
		child.stdin.on("error", () => {
			// Abort or early backend exit can close stdin before the request is fully written.
		});
		child.stdout.on("data", (chunk: Buffer) => {
			appendOutput(stdout, chunk);
			if (!settled) inspectProtocolText(stdoutDecoder.write(chunk));
		});
		child.stderr.on("data", (chunk: Buffer) => appendOutput(stderr, chunk));
		child.on("error", (error) => {
			if (settled) return;
			if (aborted) {
				terminationError ??= error.message;
				finishAbort();
				return;
			}
			settled = true;
			cleanup();
			const state = outputState();
			resolve({
				ok: false,
				data: {
					ok: false,
					error: spawned
						? `python ${command} process error: ${error.message}`
						: `failed to start python (${py}): ${error.message}`,
					command,
					...state,
				},
				raw: "",
				exitCode: null,
				...state,
			});
		});
		child.on("close", finish);
		try {
			child.stdin.end(payload, "utf8");
		} catch {
			// The stream error path carries the observable failure.
		}
		if (signal) {
			if (signal.aborted) onAbort();
			else signal.addEventListener("abort", onAbort, { once: true });
		}
	});
}

/**
 * pi-silent-gui — spawn / wait / capture / click / type / key / kill
 * on a private Windows desktop. Backend: backend/silent_gui.py
 */
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { isWindows, PY_SCRIPT, runPython } from "./bridge.ts";
import {
	cleanupRecordFromFailedSpawn,
	stringValue,
	type SessionRec,
	validateCapture,
	validateKill,
	validateMessage,
	validateSpawn,
	validateWait,
} from "./protocol.ts";

function textResult(obj: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }],
		details: (typeof obj === "object" && obj ? obj : {}) as Record<string, unknown>,
	};
}

function captureResult(obj: Record<string, unknown>, pngPath: string) {
	// 附件必须是磁盘上那份原图像素。缩放或转 JPEG 会让模型按缩略图点，而 click 仍按窗口像素走。
	// 大图撑爆上下文时再另做限边，那时附件必须带缩放比，或不再允许按图点。
	const png = fs.readFileSync(pngPath);
	return {
		content: [
			{ type: "text" as const, text: JSON.stringify(obj, null, 2) },
			{ type: "image" as const, data: png.toString("base64"), mimeType: "image/png" },
		],
		details: obj,
	};
}

function failTool(data: Record<string, unknown>): never {
	throw new Error(JSON.stringify(data, null, 2));
}

const CAPTURE_TIMEOUT_MS = 15_000;
const captureOutputLocks = new Map<string, Promise<void>>();

function cleanupCaptureTemp(file: string, attempts = 50) {
	fs.rm(file, { force: true }, (error) => {
		if (!error || (error as NodeJS.ErrnoException).code === "ENOENT" || attempts <= 0) return;
		const retry = setTimeout(() => cleanupCaptureTemp(file, attempts - 1), 100);
		retry.unref?.();
	});
}

function publicWindow(data: Record<string, unknown>) {
	const hwnd = data.hwnd;
	if (typeof hwnd !== "number") return {};
	return {
		hwnd,
		title: typeof data.title === "string" ? data.title : undefined,
		window_class: typeof data.window_class === "string" ? data.window_class : undefined,
		width: typeof data.width === "number" ? data.width : undefined,
		height: typeof data.height === "number" ? data.height : undefined,
	};
}

function publicWindowItem(value: unknown) {
	if (!value || typeof value !== "object") return undefined;
	const window = publicWindow(value as Record<string, unknown>);
	return typeof window.hwnd === "number" ? window : undefined;
}

function publicSnapshot(data: Record<string, unknown>) {
	if (typeof data.alive !== "boolean") return {};
	const exitCode =
		typeof data.exit_code === "number" && Number.isSafeInteger(data.exit_code)
			? data.exit_code
			: null;
	const windows = Array.isArray(data.windows)
		? data.windows.map(publicWindowItem).filter((item): item is NonNullable<typeof item> => !!item)
		: [];
	return { alive: data.alive, exit_code: exitCode, windows };
}

function publicKill(data: Record<string, unknown>) {
	const result: Record<string, unknown> = {
		ok: data.ok === true,
		session_id: data.session_id,
	};
	if (data.stale_only === true) result.stale_only = true;
	if (data.already_absent === true) result.already_absent = true;
	if (data.ok !== true && data.error) result.error = data.error;
	return result;
}

export default function piSilentGui(pi: ExtensionAPI) {
	// Pi 可能复用同一份 factory。登记必须停在这次调用里，否则一个 runtime 的关机
	// 会把另一个 runtime 的 Job 一起收掉。
	const sessions = new Map<string, SessionRec>();
	const cleanupToken = randomBytes(32).toString("hex");
	let cleanupRetryTimer: ReturnType<typeof setTimeout> | undefined;
	let cleanupRetryInFlight = false;
	const cleanupRetrySessions = new Set<string>();
	const cleanupInFlight = new Map<string, Promise<Record<string, unknown>>>();
	type SessionQueue = {
		tail: Promise<void>;
		closing: boolean;
		close: AbortController;
	};
	const sessionQueues = new Map<string, SessionQueue>();

	function resolveSession(params: { session_id?: string }): {
		sessionId?: string;
		rec?: SessionRec;
		error?: string;
	} {
		if (!params.session_id) return { error: "session_id required" };
		const rec = sessions.get(params.session_id);
		return rec
			? { sessionId: params.session_id, rec }
			: { error: `unknown session_id: ${params.session_id}` };
	}

	function queueFor(sessionId: string): SessionQueue {
		let queue = sessionQueues.get(sessionId);
		if (!queue) {
			queue = { tail: Promise.resolve(), closing: false, close: new AbortController() };
			sessionQueues.set(sessionId, queue);
		}
		return queue;
	}

	function remember(sessionId: string, rec: SessionRec) {
		sessions.set(sessionId, rec);
		queueFor(sessionId);
	}

	function forget(sessionId: string) {
		sessions.delete(sessionId);
		sessionQueues.delete(sessionId);
	}

	function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
		if (!signal) return promise;
		return new Promise((resolve, reject) => {
			let settled = false;
			const onAbort = () => {
				if (settled) return;
				settled = true;
				signal.removeEventListener("abort", onAbort);
				reject(signal.reason ?? new Error("operation aborted"));
			};
			promise.then(
				(value) => {
					if (settled) return;
					settled = true;
					signal.removeEventListener("abort", onAbort);
					resolve(value);
				},
				(error) => {
					if (settled) return;
					settled = true;
					signal.removeEventListener("abort", onAbort);
					reject(error);
				},
			);
			if (signal.aborted) onAbort();
			else signal.addEventListener("abort", onAbort, { once: true });
		});
	}

	function bounded<T>(promise: Promise<T>, timeoutMs: number, error: Error): Promise<T> {
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => reject(error), timeoutMs);
			timer.unref?.();
			promise.then(
				(value) => {
					clearTimeout(timer);
					resolve(value);
				},
				(reason) => {
					clearTimeout(timer);
					reject(reason);
				},
			);
		});
	}

	function enqueueSession<T>(
		sessionId: string,
		signal: AbortSignal | undefined,
		task: (signal: AbortSignal) => Promise<T>,
	): Promise<T> {
		const queue = queueFor(sessionId);
		const effectiveSignal = signal
			? AbortSignal.any([signal, queue.close.signal])
			: queue.close.signal;
		const run = queue.tail.then(async () => {
			effectiveSignal.throwIfAborted();
			if (queue.closing) {
				failTool({ ok: false, error: "session kill in progress", session_id: sessionId });
			}
			return task(effectiveSignal);
		});
		queue.tail = run.then(
			() => undefined,
			() => undefined,
		);
		return abortable(run, effectiveSignal);
	}

	async function withCaptureOutputLock<T>(
		outPath: string,
		signal: AbortSignal,
		task: () => Promise<T>,
	): Promise<T> {
		const normalized = path.normalize(path.resolve(outPath)).toLowerCase();
		const previous = captureOutputLocks.get(normalized) ?? Promise.resolve();
		let release!: () => void;
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		const tail = previous.then(
			() => held,
			() => held,
		);
		captureOutputLocks.set(normalized, tail);
		try {
			await abortable(previous, signal);
			signal.throwIfAborted();
			return await task();
		} finally {
			release();
			const clear = () => {
				if (captureOutputLocks.get(normalized) === tail) captureOutputLocks.delete(normalized);
			};
			tail.then(clear, clear);
		}
	}

	function withSessionWork<T>(
		sessionId: string,
		signal: AbortSignal | undefined,
		task: (signal: AbortSignal) => Promise<T>,
	): Promise<T> {
		const queue = queueFor(sessionId);
		if (queue.closing) {
			failTool({ ok: false, error: "session kill in progress", session_id: sessionId });
		}
		return enqueueSession(sessionId, signal, task);
	}

	function registeredPayload(sessionId: string, rec: SessionRec, extra: Record<string, unknown>) {
		return {
			session_id: sessionId,
			_job_name: rec.jobName,
			_desktop: rec.desktop,
			_broker_pid: rec.brokerPid,
			_broker_created: rec.brokerCreated,
			_root_pid: rec.pid,
			_root_created: rec.rootCreated,
			_input_mode: rec.inputMode,
			cleanup_token: cleanupToken,
			...extra,
		};
	}

	function killRegistered(sessionId: string, signal?: AbortSignal) {
		if (signal?.aborted) return Promise.reject(signal.reason ?? new Error("operation aborted"));
		const queue = queueFor(sessionId);
		queue.closing = true;
		if (!queue.close.signal.aborted) {
			queue.close.abort(
				new Error(JSON.stringify({ ok: false, error: "session kill in progress", session_id: sessionId })),
			);
		}
		let operation = cleanupInFlight.get(sessionId);
		if (!operation) {
			operation = (async () => {
				const rec = sessions.get(sessionId);
				const timeoutSignal = AbortSignal.timeout(7000);
				let result;
				try {
					result = await abortable(
						runPython(
							"kill",
							rec
								? {
										session_id: sessionId,
										_job_name: rec.jobName,
										_broker_pid: rec.brokerPid,
										_broker_created: rec.brokerCreated,
										cleanup_token: cleanupToken,
									}
								: { session_id: sessionId, cleanup_token: cleanupToken },
							timeoutSignal,
						),
						timeoutSignal,
					);
				} catch (error) {
					return {
						ok: false,
						error: `session cleanup timed out: ${String(error)}`,
						session_id: sessionId,
					};
				}
				if (result.data.protocol_error === "exit_status_mismatch") {
					return result.data;
				}
				try {
					validateKill(result.data, sessionId);
				} catch (error) {
					return {
						ok: false,
						error: String(error),
						session_id: sessionId,
						backend: result.data,
					};
				}
				if (result.ok) {
					cleanupRetrySessions.delete(sessionId);
					forget(sessionId);
				}
				return result.data;
			})();
			cleanupInFlight.set(sessionId, operation);
			const clear = () => {
				if (cleanupInFlight.get(sessionId) === operation) cleanupInFlight.delete(sessionId);
				if (!sessions.has(sessionId)) sessionQueues.delete(sessionId);
			};
			operation.then(clear, clear);
			operation.then(
				(result) => {
					if (result.ok !== true && sessions.has(sessionId)) scheduleCleanupRetry(sessionId);
				},
				() => {
					if (sessions.has(sessionId)) scheduleCleanupRetry(sessionId);
				},
			);
		}
		return abortable(operation, signal);
	}

	function scheduleCleanupRetry(sessionId?: string) {
		if (sessionId) cleanupRetrySessions.add(sessionId);
		if (cleanupRetryTimer || cleanupRetryInFlight || cleanupRetrySessions.size === 0) return;
		cleanupRetryTimer = setTimeout(async () => {
			cleanupRetryTimer = undefined;
			cleanupRetryInFlight = true;
			try {
				for (const retrySessionId of [...cleanupRetrySessions]) {
					if (!sessions.has(retrySessionId)) {
						cleanupRetrySessions.delete(retrySessionId);
						continue;
					}
					try {
						const result = await killRegistered(retrySessionId);
						if (result.ok === true) cleanupRetrySessions.delete(retrySessionId);
					} catch {
						/* keep this id for the next window */
					}
				}
			} finally {
				cleanupRetryInFlight = false;
				scheduleCleanupRetry();
			}
		}, 1000);
		cleanupRetryTimer.unref?.();
	}

	function requireWindows() {
		if (!isWindows()) failTool({ ok: false, error: "Windows only" });
	}

	function requireSession(params: { session_id?: string }) {
		requireWindows();
		const resolved = resolveSession(params);
		if (!resolved.sessionId || !resolved.rec) failTool({ ok: false, error: resolved.error });
		return { sessionId: resolved.sessionId, rec: resolved.rec };
	}

	pi.registerTool({
		name: "silent_spawn",
		label: "Silent Spawn",
		description:
			"Start a GUI on a private desktop that does not steal the user's screen. Returns session_id and input_mode: 'inject' can drive polling engines and hold keys; 'message' only reaches windows that honor window messages. Pass inject_dll64/inject_dll32 (path to a payload DLL) to enable injection, or omit for message mode. Call silent_wait for a window.",
		parameters: Type.Object({
			exe: Type.String({ description: "Executable path or name, e.g. notepad.exe" }),
			cwd: Type.Optional(Type.String({ description: "Working directory; defaults to this call's cwd" })),
			args: Type.Optional(Type.Array(Type.String())),
			allow_elevated: Type.Optional(
				Type.Boolean({
					description: "Only when Pi is already elevated; never opens UAC",
				}),
			),
			inject_dll64: Type.Optional(
				Type.String({
					description:
						"Path to the 64-bit payload DLL to inject so clicks/keys drive polling engines. Overrides the PI_SILENT_GUI_INJECT_DLL64 env default; omit both for message mode.",
				}),
			),
			inject_dll32: Type.Optional(
				Type.String({
					description:
						"Path to the 32-bit payload DLL, used when the target is 32-bit (common for galgame). Overrides PI_SILENT_GUI_INJECT_DLL32.",
				}),
			),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			requireWindows();
			if (!fs.existsSync(PY_SCRIPT)) {
				failTool({ ok: false, error: `missing ${PY_SCRIPT}` });
			}
			const baseCwd = ctx?.cwd ?? process.cwd();
			const targetCwd =
				params.cwd === undefined
					? baseCwd
					: path.isAbsolute(params.cwd)
						? params.cwd
						: path.resolve(baseCwd, params.cwd);
			const result = await runPython(
				"spawn",
				{
					exe: params.exe,
					cwd: targetCwd,
					args: params.args,
					allow_elevated: params.allow_elevated,
					inject_dll32: params.inject_dll32,
					inject_dll64: params.inject_dll64,
					owner_pid: process.pid,
					cleanup_token: cleanupToken,
				},
				signal,
				false,
			);
			if (!result.ok) {
				const nested = result.data.backend;
				const cleanupSource =
					nested && typeof nested === "object" && !Array.isArray(nested)
						? (nested as Record<string, unknown>)
						: result.data;
				const cleanupRecord = cleanupRecordFromFailedSpawn(cleanupSource, params.exe);
				let cleanup: unknown;
				if (cleanupRecord) {
					remember(cleanupRecord.sessionId, cleanupRecord.rec);
					cleanup = publicKill(await killRegistered(cleanupRecord.sessionId));
				}
				failTool({
					ok: false,
					error: result.data.error ?? "spawn failed",
					session_id: stringValue(cleanupSource.session_id),
					cleanup,
					protocol_error: result.data.protocol_error,
					backend: result.data.backend,
					tmp_dir: cleanupSource.tmp_dir,
					orphan_cleanup_required: result.data.orphan_cleanup_required,
					orphan_cleanup_registered: cleanupRecord
						? sessions.has(cleanupRecord.sessionId)
						: false,
				});
			}
			let validated: { sessionId: string; rec: SessionRec };
			try {
				validated = validateSpawn({ ...result.data, exe: params.exe });
			} catch (error) {
				const cleanupRecord = cleanupRecordFromFailedSpawn(result.data, params.exe);
				let cleanup: unknown;
				if (cleanupRecord) {
					remember(cleanupRecord.sessionId, cleanupRecord.rec);
					cleanup = publicKill(await killRegistered(cleanupRecord.sessionId));
				}
				failTool({
					ok: false,
					error: String(error),
					session_id: stringValue(result.data.session_id),
					cleanup,
					tmp_dir: result.data.tmp_dir,
					orphan_cleanup_required: result.data.orphan_cleanup_required,
					orphan_cleanup_registered: cleanupRecord
						? sessions.has(cleanupRecord.sessionId)
						: false,
				});
			}
			remember(validated.sessionId, validated.rec);
			if (signal?.aborted) {
				const cleanup = publicKill(await killRegistered(validated.sessionId));
				failTool({
					ok: false,
					error: "silent_spawn aborted",
					session_id: validated.sessionId,
					cleanup,
				});
			}
			return textResult({
				session_id: validated.sessionId,
				pid: validated.rec.pid,
				tmp_dir: validated.rec.tmpDir,
				registered: true,
				input_mode: validated.rec.inputMode,
				...publicWindow(result.data),
			});
		},
	});

	pi.registerTool({
		name: "silent_wait",
		label: "Silent Wait",
		description:
			"Wait until a matching window exists. Returns input_mode and the session snapshot: alive, exit_code, and all top-level windows. Failures include the same snapshot.",
		parameters: Type.Object({
			session_id: Type.String(),
			title: Type.Optional(Type.String({ description: "Title substring" })),
			window_class: Type.Optional(Type.String()),
			hwnd: Type.Optional(Type.Integer({ minimum: 1 })),
			timeout_ms: Type.Optional(Type.Integer({ minimum: 100, maximum: 60_000 })),
		}),
		async execute(_id, params, signal) {
			const { sessionId, rec } = requireSession(params);
			return withSessionWork(sessionId, signal, async (operationSignal) => {
				const result = await runPython(
					"wait",
					registeredPayload(sessionId, rec, {
						title: params.title,
						window_class: params.window_class,
						hwnd: params.hwnd,
						timeout_ms: params.timeout_ms,
					}),
					operationSignal,
				);
				if (!result.ok) {
					failTool({
						ok: false,
						error: result.data.error ?? "wait failed",
						session_id: sessionId,
						...publicSnapshot(result.data),
					});
				}
				try {
					validateWait(result.data, sessionId);
				} catch (error) {
					failTool({ ok: false, error: String(error) });
				}
				return textResult({
					session_id: sessionId,
					input_mode: rec.inputMode,
					...publicWindow(result.data),
					...publicSnapshot(result.data),
				});
			});
		},
	});

	pi.registerTool({
		name: "silent_capture",
		label: "Silent Capture",
		description:
			"Capture the session window as a lossless PNG and attach that same image. Click x/y use this image: (0,0) is the top-left of the window.",
		parameters: Type.Object({
			session_id: Type.String(),
			title: Type.Optional(Type.String({ description: "Title substring" })),
			window_class: Type.Optional(Type.String()),
			hwnd: Type.Optional(Type.Integer({ minimum: 1 })),
			out_path: Type.Optional(Type.String()),
			overwrite: Type.Optional(
				Type.Boolean({ description: "Replace out_path if it exists; default false" }),
			),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { sessionId, rec } = requireSession(params);
			const outPath = params.out_path
				? path.isAbsolute(params.out_path)
					? params.out_path
					: path.resolve(ctx?.cwd ?? process.cwd(), params.out_path)
				: path.join(rec.tmpDir, `cap_${Date.now()}_${randomBytes(4).toString("hex")}.png`);
			const pendingPath = path.join(
				path.dirname(outPath),
				`.${path.basename(outPath)}.${randomBytes(16).toString("hex")}.tmp`,
			);
			const overwrite = params.overwrite ?? false;
			return withSessionWork(sessionId, signal, async (operationSignal) => {
				const executeCapture = async () => {
					let helperSucceeded = false;
					try {
						const timeoutSignal = AbortSignal.timeout(CAPTURE_TIMEOUT_MS);
						const effectiveSignal = AbortSignal.any([operationSignal, timeoutSignal]);
						let result;
						try {
							result = await bounded(
								runPython(
									"capture",
									registeredPayload(sessionId, rec, {
										title: params.title,
										window_class: params.window_class,
										hwnd: params.hwnd,
										out_path: outPath,
										_pending_path: pendingPath,
										overwrite,
									}),
									effectiveSignal,
								),
								CAPTURE_TIMEOUT_MS + 1000,
								new Error("capture helper did not settle after its hard timeout"),
							);
						} catch (error) {
							if (timeoutSignal.aborted && !operationSignal.aborted) {
								failTool({
									ok: false,
									error: `capture timed out after ${CAPTURE_TIMEOUT_MS}ms`,
									session_id: sessionId,
								});
							}
							throw error;
						}
						if (timeoutSignal.aborted && !operationSignal.aborted) {
							failTool({
								ok: false,
								error: `capture timed out after ${CAPTURE_TIMEOUT_MS}ms`,
								session_id: sessionId,
							});
						}
						if (!result.ok) {
							failTool({
								ok: false,
								error: result.data.error ?? "capture failed",
								session_id: sessionId,
								...publicSnapshot(result.data),
							});
						}
						helperSucceeded = true;
						try {
							validateCapture(result.data, sessionId, outPath);
						} catch (error) {
							failTool({ ok: false, error: String(error) });
						}
						const publicCapture = {
							session_id: sessionId,
							path: result.data.path,
							all_black: result.data.all_black,
							client: (result.data.window as { client?: unknown } | undefined)?.client,
							...publicWindow(result.data),
						};
						return captureResult(publicCapture, String(result.data.path));
					} finally {
						if (!helperSucceeded) cleanupCaptureTemp(pendingPath);
					}
				};
				return overwrite
					? withCaptureOutputLock(outPath, operationSignal, executeCapture)
					: executeCapture();
			});
		},
	});

	function inputTool(
		name: string,
		label: string,
		description: string,
		parameters: ReturnType<typeof Type.Object>,
		toRequest: (params: Record<string, unknown>) => Record<string, unknown>,
		toPublic: (data: Record<string, unknown>, request: Record<string, unknown>) => Record<string, unknown>,
	) {
		pi.registerTool({
			name,
			label,
			description,
			parameters,
			async execute(_id, params, signal) {
				const { sessionId, rec } = requireSession(params);
				const request = toRequest(params as Record<string, unknown>);
				return withSessionWork(sessionId, signal, async (operationSignal) => {
					const result = await runPython(
						"message",
						registeredPayload(sessionId, rec, request),
						operationSignal,
					);
					if (!result.ok) {
						failTool({
							ok: false,
							error: result.data.error ?? `${name} failed`,
							session_id: sessionId,
							...publicSnapshot(result.data),
						});
					}
					try {
						validateMessage(result.data, sessionId, request);
					} catch (error) {
						failTool({ ok: false, error: String(error) });
					}
					return textResult(toPublic(result.data, request));
				});
			},
		});
	}

	inputTool(
		"silent_click",
		"Silent Click",
		"Left-click using coordinates from the last silent_capture PNG. (0,0) is the window top-left. Repeat the same point with count. In a message-mode session the click is a window message that engines polling raw input (many Unity/DirectInput games) may ignore; if two or three clicks change nothing on the next capture, stop and report instead of retrying.",
		Type.Object({
			session_id: Type.String(),
			x: Type.Integer({ description: "PNG x coordinate" }),
			y: Type.Integer({ description: "PNG y coordinate" }),
			count: Type.Optional(
				Type.Integer({ minimum: 1, maximum: 50, description: "Repeat this click; default 1" }),
			),
			interval_ms: Type.Optional(
				Type.Integer({
					minimum: 1,
					maximum: 2_000,
					description: "Delay between repeats; default 300 when count > 1",
				}),
			),
			title: Type.Optional(Type.String()),
			window_class: Type.Optional(Type.String()),
			hwnd: Type.Optional(Type.Integer({ minimum: 1 })),
		}),
		(params) => ({
			action: "click",
			x: params.x,
			y: params.y,
			count: params.count,
			interval_ms: params.interval_ms,
			title: params.title,
			window_class: params.window_class,
			hwnd: params.hwnd,
		}),
		(data, request) => ({
			session_id: data.session_id,
			x: request.x,
			y: request.y,
			count: data.count,
			...publicWindow(data),
		}),
	);

	inputTool(
		"silent_type",
		"Silent Type",
		"Type text into the focused control of the session window. Newlines send Return.",
		Type.Object({
			session_id: Type.String(),
			text: Type.String({ description: "Text to type, up to 4000 characters" }),
			title: Type.Optional(Type.String()),
			window_class: Type.Optional(Type.String()),
			hwnd: Type.Optional(Type.Integer({ minimum: 1 })),
		}),
		(params) => {
			if (typeof params.text !== "string" || params.text.length === 0) {
				failTool({ ok: false, error: "type requires text" });
			}
			return {
				action: "type",
				text: params.text,
				title: params.title,
				window_class: params.window_class,
				hwnd: params.hwnd,
			};
		},
		(data) => ({
			session_id: data.session_id,
			chars: data.chars,
			...publicWindow(data),
		}),
	);

	inputTool(
		"silent_key",
		"Silent Key",
		"Press a named key such as return, tab, escape, backspace, left, or delete. Repeat the same key with count. In an inject-mode session, hold_ms holds the key down (e.g. hold control to skip); message-mode keys may be ignored by engines that poll raw input.",
		Type.Object({
			session_id: Type.String(),
			key: Type.String({ description: "Key name, e.g. return, tab, escape" }),
			count: Type.Optional(
				Type.Integer({ minimum: 1, maximum: 50, description: "Repeat this key; default 1" }),
			),
			interval_ms: Type.Optional(
				Type.Integer({
					minimum: 1,
					maximum: 2_000,
					description: "Delay between repeats; default 300 when count > 1",
				}),
			),
			hold_ms: Type.Optional(
				Type.Integer({
					minimum: 1,
					maximum: 10_000,
					description:
						"Inject sessions only: hold the key down this long, e.g. hold control to skip. Rejected in message sessions.",
				}),
			),
			title: Type.Optional(Type.String()),
			window_class: Type.Optional(Type.String()),
			hwnd: Type.Optional(Type.Integer({ minimum: 1 })),
		}),
		(params) => {
			if (typeof params.key !== "string" || params.key.length === 0) {
				failTool({ ok: false, error: "key required" });
			}
			return {
				action: "key",
				key: params.key,
				count: params.count,
				interval_ms: params.interval_ms,
				hold_ms: params.hold_ms,
				title: params.title,
				window_class: params.window_class,
				hwnd: params.hwnd,
			};
		},
		(data, request) => ({
			session_id: data.session_id,
			key: request.key,
			count: data.count,
			...publicWindow(data),
		}),
	);

	pi.registerTool({
		name: "silent_kill",
		label: "Silent Kill",
		description: "Stop the session process tree and remove its temp directory.",
		parameters: Type.Object({
			session_id: Type.String(),
		}),
		async execute(_id, params, signal) {
			requireWindows();
			if (!params.session_id) failTool({ ok: false, error: "session_id required" });
			const data = await killRegistered(params.session_id, signal);
			if (data.ok !== true) failTool(publicKill(data));
			return textResult(publicKill(data));
		},
	});

	pi.on("session_shutdown", async () => {
		const deadline = Date.now() + 7000;
		for (const sessionId of sessions.keys()) cleanupRetrySessions.add(sessionId);
		scheduleCleanupRetry();
		const cleanup = Promise.all(
			[...sessions.keys()].map(async (sessionId) => {
				try {
					await killRegistered(sessionId);
				} catch {
					/* retained registry retries after this bounded shutdown */
				}
			}),
		);
		let timer: ReturnType<typeof setTimeout> | undefined;
		await Promise.race([
			cleanup,
			new Promise<void>((resolve) => {
				timer = setTimeout(resolve, Math.max(0, deadline - Date.now()));
				timer.unref?.();
			}),
		]);
		if (timer) clearTimeout(timer);
		scheduleCleanupRetry();
	});
}

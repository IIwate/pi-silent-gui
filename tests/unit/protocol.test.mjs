import assert from "node:assert/strict";
import { ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadTs } from "../support/load-ts.mjs";

const { validateKill, validateMessage, validateWait } = await loadTs("src/protocol.ts");
const {
	PYTHON_PROTOCOL_PREFIX,
	PYTHON_PROTOCOL_VERSION,
	runPython,
} = await loadTs("src/bridge.ts");
const sid = "0123456789ab";
const hwnd = 100;
const window = {
	hwnd,
	width: 640,
	height: 480,
	dpi: 120,
	client: { x: 3, y: 30, width: 634, height: 447 },
};
const clientClick = (x, y) => ({
	window: structuredClone(window),
	hit_test: 1,
	target_hwnd: 200,
	dispatch: "client",
	point: {
		window: { x, y },
		screen: { x: -100 + x, y: -50 + y },
		target: { x: x - 10, y: y - 20 },
		target_space: "client",
	},
});
const message = (click) => ({
	ok: true,
	session_id: sid,
	hwnd,
	action: "click",
	coordinate_space: "window",
	window: structuredClone(window),
	...click,
});

function protocolShim(t, mode = "normal") {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-protocol-"));
	const previousPythonPath = process.env.PYTHONPATH;
	const previousMode = process.env.PI_SILENT_GUI_TEST_FRAME_MODE;
	fs.writeFileSync(
		path.join(directory, "sitecustomize.py"),
		[
			"import json, os, sys, time",
			"_stdout = sys.stdout",
			"_mode = os.environ.get('PI_SILENT_GUI_TEST_FRAME_MODE', 'normal')",
			"if _mode == 'noise_only':",
			"    _stdout.buffer.write((b'\\xe6\\xb5\\x8b' * 400000) + b'\\n')",
			"    _stdout.buffer.flush()",
			"if _mode == 'hang':",
			"    time.sleep(1.5)",
			"class _FramedStdout:",
			"    def write(self, text):",
			`        if not text.startswith('${PYTHON_PROTOCOL_PREFIX}\\t${PYTHON_PROTOCOL_VERSION}\\t'):`,
			"            return _stdout.write(text)",
			"        if _mode == 'noise_only':",
			"            return len(text)",
			"        if _mode == 'oversized_frame':",
			`            _stdout.write('${PYTHON_PROTOCOL_PREFIX}\\t${PYTHON_PROTOCOL_VERSION}\\t' + sys.argv[1] + '\\t' + json.dumps({'ok': True, 'pad': 'x' * (1024 * 1024 + 1)}) + '\\n')`,
			"            return len(text)",
			"        _stdout.write(text)",
			"        command = sys.argv[1]",
			"        if _mode == 'trailing_json':",
			"            _stdout.write('{\"ok\":false,\"noise\":true}\\n')",
			"        elif _mode == 'trailing_prefixed':",
			`            _stdout.write('${PYTHON_PROTOCOL_PREFIX}\\t${PYTHON_PROTOCOL_VERSION}\\t' + command + '\\t{\"ok\":false,\"noise\":true}\\n')`,
			"        elif _mode == 'duplicate':",
			"            _stdout.write(text)",
			"        return len(text)",
			"    def flush(self):",
			"        return _stdout.flush()",
			"    def __getattr__(self, name):",
			"        return getattr(_stdout, name)",
			"sys.stdout = _FramedStdout()",
		].join("\n"),
		"utf8",
	);
	process.env.PYTHONPATH = [directory, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_TEST_FRAME_MODE = mode;
	t.after(async () => {
		if (mode === "hang") await new Promise((resolve) => setTimeout(resolve, 1700));
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		if (previousMode === undefined) delete process.env.PI_SILENT_GUI_TEST_FRAME_MODE;
		else process.env.PI_SILENT_GUI_TEST_FRAME_MODE = previousMode;
		fs.rmSync(directory, { recursive: true, force: true });
	});
}

test("accepts a click inside the window", () => {
	assert.doesNotThrow(() =>
		validateMessage(message(clientClick(100, 120)), sid, {
			action: "click",
			x: 100,
			y: 120,
		}),
	);
});

test("rejects a repeat count that does not match the request", () => {
	assert.throws(
		() =>
			validateMessage({ ...message(clientClick(100, 120)), count: 1 }, sid, {
				action: "click",
				x: 100,
				y: 120,
				count: 3,
			}),
		/invalid repeat count/,
	);
});

test("rejects a click outside the window and a hwnd mismatch", () => {
	assert.throws(
		() =>
			validateMessage(message(clientClick(100, 120)), sid, {
				action: "click",
				x: 640,
				y: 120,
			}),
		/click outside window/,
	);
	assert.throws(
		() =>
			validateMessage(message(clientClick(100, 120)), sid, {
				action: "click",
				x: 100,
				y: 120,
				hwnd: hwnd + 1,
			}),
		/hwnd mismatch/,
	);
});

test("accepts type and wait without dispatch metadata", () => {
	assert.doesNotThrow(() =>
		validateMessage({ ok: true, session_id: sid, hwnd, action: "type", chars: 3 }, sid, {
			action: "type",
			text: "abc",
		}),
	);
	assert.doesNotThrow(() =>
		validateWait(
			{ ok: true, session_id: sid, hwnd, alive: true, exit_code: null, windows: [] },
			sid,
		),
	);
	assert.throws(
		() => validateWait({ ok: true, session_id: sid, hwnd }, sid),
		/invalid session snapshot/,
	);
});

test("hwnd request still binds the response window", () => {
	const click = message(clientClick(100, 120));
	assert.doesNotThrow(() =>
		validateMessage(click, sid, {
			action: "click",
			x: 100,
			y: 120,
			hwnd,
		}),
	);
	assert.throws(
		() =>
			validateMessage(click, sid, {
				action: "click",
				x: 100,
				y: 120,
				hwnd: hwnd + 1,
			}),
		/hwnd mismatch/,
	);
});

test("bridge rejects request payloads over the size limit", async () => {
	const result = await runPython("message", { pad: "x".repeat(1024 * 1024) });
	assert.equal(result.ok, false);
	assert.equal(result.data.error, "request payload exceeds size limit");
	assert.equal(result.data.command, "message");
	assert.equal(result.exitCode, null);
});

test("bridge ignores trailing JSON after one explicit protocol frame", async (t) => {
	protocolShim(t, "trailing_json");
	const sessionId = randomBytes(6).toString("hex");
	const result = await runPython("kill", { session_id: sessionId });
	assert.equal(result.ok, true, JSON.stringify(result.data));
	assert.equal(result.data.session_id, sessionId);
	assert.equal(result.data.noise, undefined);
});

for (const mode of ["trailing_prefixed", "duplicate"]) {
	test(`bridge rejects ${mode.replace("_", " ")} frames`, async (t) => {
		protocolShim(t, mode);
		const result = await runPython("kill", {
			session_id: randomBytes(6).toString("hex"),
		});
		assert.equal(result.ok, false);
		assert.equal(result.data.protocol_error, "duplicate_frame");
	});
}

test("bridge rejects an oversized protocol frame before validation", async (t) => {
	protocolShim(t, "oversized_frame");
	const result = await runPython("kill", { session_id: randomBytes(6).toString("hex") });
	assert.equal(result.ok, false);
	assert.equal(result.data.protocol_error, "frame_too_large");
});

test("bridge bounds retained stdout by UTF-8 bytes", async (t) => {
	protocolShim(t, "noise_only");
	const result = await runPython("kill", {
		session_id: randomBytes(6).toString("hex"),
	});
	assert.equal(result.ok, false);
	assert.ok(Buffer.byteLength(result.data.stdout, "utf8") <= 1024 * 1024);
	assert.equal(result.stdoutTruncated, true);
});

test("bridge settles an abort when child.kill throws", async (t) => {
	protocolShim(t, "hang");
	const originalKill = ChildProcess.prototype.kill;
	ChildProcess.prototype.kill = function () {
		throw new Error("injected kill failure");
	};
	t.after(() => {
		ChildProcess.prototype.kill = originalKill;
	});
	const abort = new AbortController();
	setTimeout(() => abort.abort(new Error("test abort")), 50);
	const started = Date.now();
	const result = await runPython(
		"kill",
		{ session_id: randomBytes(6).toString("hex") },
		abort.signal,
	);
	assert.ok(Date.now() - started < 1500);
	assert.equal(result.ok, false);
	assert.equal(result.data.protocol_error, "aborted");
	assert.equal(result.data.termination_error, "injected kill failure");
	assert.doesNotMatch(result.data.error, /failed to start python/);
});

test("kill accepts success and failure as long as session_id and ok are present", () => {
	assert.doesNotThrow(() => validateKill({ ok: true, session_id: sid }, sid));
	assert.doesNotThrow(() =>
		validateKill({ ok: false, session_id: sid, error: "cleanup incomplete" }, sid),
	);
	assert.throws(() => validateKill({ session_id: sid }, sid));
});

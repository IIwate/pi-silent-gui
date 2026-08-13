import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadPiEntry, loadTs } from "../support/load-ts.mjs";

if (process.platform !== "win32") throw new Error("Windows only");

const TEST_GUI_EXE = process.env.PI_SILENT_GUI_TEST_EXE || "notepad.exe";
const extension = await loadPiEntry();
const { runPython } = await loadTs("src/bridge.ts");

function createRuntime() {
	const tools = [];
	const handlers = new Map();
	extension.default({
		registerTool: (tool) => tools.push(tool),
		on: (event, handler) => handlers.set(event, handler),
	});
	return {
		tools,
		tool: (name) => tools.find((item) => item.name === name),
		shutdown: () => handlers.get("session_shutdown")?.(),
	};
}

const primary = createRuntime();
const tools = primary.tools;
const tool = primary.tool;
const spawn = tool("silent_spawn");
const wait = tool("silent_wait");
const capture = tool("silent_capture");
const click = tool("silent_click");
const typeText = tool("silent_type");
const key = tool("silent_key");
const kill = tool("silent_kill");
const body = (result) => JSON.parse(result.content[0].text);

function toolError(error) {
	try {
		return JSON.parse(error.message);
	} catch {
		throw error;
	}
}

function pngSize(file) {
	const header = Buffer.alloc(24);
	const fd = fs.openSync(file, "r");
	try {
		assert.equal(fs.readSync(fd, header, 0, header.length, 0), header.length);
	} finally {
		fs.closeSync(fd);
	}
	assert.deepEqual(header.subarray(0, 8), Buffer.from("89504e470d0a1a0a", "hex"));
	return [header.readUInt32BE(16), header.readUInt32BE(20)];
}

test("package manifest loads Google-compatible Pi tool schemas", () => {
	assert.deepEqual(
		tools.map((item) => item.name),
		[
			"silent_spawn",
			"silent_wait",
			"silent_capture",
			"silent_click",
			"silent_type",
			"silent_key",
			"silent_kill",
		],
	);
	for (const name of [
		"silent_wait",
		"silent_capture",
		"silent_click",
		"silent_type",
		"silent_key",
		"silent_kill",
	]) {
		const schema = tool(name).parameters;
		assert.equal(schema.properties.pid, undefined);
		assert.ok(schema.required?.includes("session_id"));
	}
	assert.equal(spawn.parameters.properties.clean_env, undefined);
	assert.equal(spawn.parameters.properties.audio_device_policy, undefined);
	assert.equal(typeText.parameters.properties.text.type, "string");
	assert.equal(key.parameters.properties.key.type, "string");
	assert.equal(click.parameters.properties.x.type, "integer");
	assert.equal(click.parameters.properties.count.maximum, 50);
	assert.equal(key.parameters.properties.interval_ms.maximum, 2_000);
	assert.equal(wait.parameters.properties.timeout_ms.maximum, 60_000);
	assert.equal(capture.parameters.properties.overwrite.type, "boolean");
});

test("public kill removes only a dead-owner stale directory with a foreign token", async (t) => {
	const runtime = createRuntime();
	const sessionId = randomBytes(6).toString("hex");
	const tmpDir = path.join(process.env.LOCALAPPDATA, "Temp", "pi-silent-gui", sessionId);
	const oldTokenHash = createHash("sha256").update("a".repeat(64)).digest("hex");
	const jobSuffix = createHash("sha256")
		.update(`${oldTokenHash}:job:${sessionId}`)
		.digest("hex")
		.slice(0, 32);
	t.after(async () => {
		await runtime.shutdown();
		fs.rmSync(tmpDir, { recursive: true, force: true });
	});
	fs.mkdirSync(path.dirname(tmpDir), { recursive: true });
	fs.mkdirSync(tmpDir);
	fs.writeFileSync(
		path.join(tmpDir, "session.json"),
		JSON.stringify({
			status: "ready",
			session_id: sessionId,
			desktop: `pi_silent_${sessionId}_${"1".repeat(32)}`,
			job_name: `pi_silent_job_${sessionId}_${jobSuffix}`,
			broker_pid: 99999991,
			broker_created: "1",
			pid: 99999992,
			root_created: "1",
			owner_pid: 99999993,
			owner_created: "1",
			cleanup_token_hash: oldTokenHash,
		}),
		"utf8",
	);

	const cleaned = body(
		await runtime.tool("silent_kill").execute(
			"stale-only",
			{ session_id: sessionId },
			new AbortController().signal,
		),
	);
	assert.equal(cleaned.stale_only, true);
	assert.equal(fs.existsSync(tmpDir), false);
});

test("factory runtimes clean only their own registered sessions", async (t) => {
	const runtimeA = createRuntime();
	const runtimeB = createRuntime();
	let sessionA;
	let sessionB;

	async function forceCleanup(runtime, session) {
		if (!session || !fs.existsSync(session.tmp_dir)) return;
		await runtime.tool("silent_kill").execute(
			"runtime-isolation-cleanup",
			{ session_id: session.session_id },
			new AbortController().signal,
		);
	}

	t.after(async () => {
		await forceCleanup(runtimeA, sessionA);
		await forceCleanup(runtimeB, sessionB);
	});

	sessionA = body(
		await runtimeA.tool("silent_spawn").execute(
			"runtime-a",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	sessionB = body(
		await runtimeB.tool("silent_spawn").execute(
			"runtime-b",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);

	await assert.rejects(
		runtimeB.tool("silent_kill").execute(
			"runtime-b-cross-kill",
			{ session_id: sessionA.session_id },
			new AbortController().signal,
		),
		(error) => toolError(error).error.includes("runtime capability mismatch"),
	);
	assert.equal(fs.existsSync(sessionA.tmp_dir), true);
	const capturedA = body(
		await runtimeA.tool("silent_capture").execute(
			"runtime-a-capture",
			{ session_id: sessionA.session_id },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	assert.equal(capturedA.session_id, sessionA.session_id);

	await runtimeA.shutdown();
	assert.equal(fs.existsSync(sessionA.tmp_dir), false);
	assert.equal(fs.existsSync(sessionB.tmp_dir), true);

	const capturedB = body(
		await runtimeB.tool("silent_capture").execute(
			"runtime-b-capture",
			{ session_id: sessionB.session_id },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	assert.equal(capturedB.session_id, sessionB.session_id);

	const killedB = body(
		await runtimeB.tool("silent_kill").execute(
			"runtime-b-kill",
			{ session_id: sessionB.session_id },
			new AbortController().signal,
		),
	);
	assert.equal(killedB.ok, true);
	assert.equal(fs.existsSync(sessionB.tmp_dir), false);
});

test("stop write failure does not block shutdown cleanup", async (t) => {
	const runtime = createRuntime();
	let session;
	let blocker;

	t.after(async () => {
		if (blocker && fs.existsSync(blocker)) fs.rmdirSync(blocker);
		if (!session || !fs.existsSync(session.tmp_dir)) return;
		await runtime.tool("silent_kill").execute(
			"retry-fallback-cleanup",
			{ session_id: session.session_id },
			new AbortController().signal,
		);
	});

	session = body(
		await runtime.tool("silent_spawn").execute(
			"retry-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	blocker = path.join(session.tmp_dir, "stop");
	fs.mkdirSync(blocker);

	await Promise.all([runtime.shutdown(), runtime.shutdown()]);
	const deadline = Date.now() + 5000;
	while (fs.existsSync(session.tmp_dir) && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
	assert.equal(fs.existsSync(session.tmp_dir), false);
});

test("runtime shutdown bounds a hung cleanup before retained retry", async (t) => {
	const runtime = createRuntime();
	const hangDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-hang-"));
	const marker = path.join(hangDir, "entered.txt");
	const previousPythonPath = process.env.PYTHONPATH;
	const previousMarker = process.env.PI_SILENT_GUI_TEST_HANG_MARKER;
	let session;

	t.after(async () => {
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		if (previousMarker === undefined) delete process.env.PI_SILENT_GUI_TEST_HANG_MARKER;
		else process.env.PI_SILENT_GUI_TEST_HANG_MARKER = previousMarker;
		if (session && fs.existsSync(session.tmp_dir)) {
			await runtime.tool("silent_kill").execute(
				"hung-cleanup-fallback",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(hangDir, { recursive: true, force: true });
	});

	fs.writeFileSync(
		path.join(hangDir, "sitecustomize.py"),
		[
			"import os, pathlib, sys, time",
			"if len(sys.argv) > 1 and sys.argv[1] == 'kill':",
			"    pathlib.Path(os.environ['PI_SILENT_GUI_TEST_HANG_MARKER']).write_text('entered', encoding='utf-8')",
			"    time.sleep(30)",
		].join("\n"),
	);
	session = body(
		await runtime.tool("silent_spawn").execute(
			"hung-cleanup-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	process.env.PYTHONPATH = [hangDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_TEST_HANG_MARKER = marker;

	const started = Date.now();
	await runtime.shutdown();
	const elapsed = Date.now() - started;
	assert.equal(fs.existsSync(marker), true);
	assert.ok(elapsed >= 6000 && elapsed < 10000, `cleanup timeout was ${elapsed}ms`);

	if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
	else process.env.PYTHONPATH = previousPythonPath;
	if (previousMarker === undefined) delete process.env.PI_SILENT_GUI_TEST_HANG_MARKER;
	else process.env.PI_SILENT_GUI_TEST_HANG_MARKER = previousMarker;
	const deadline = Date.now() + 10000;
	while (fs.existsSync(session.tmp_dir) && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
	assert.equal(fs.existsSync(session.tmp_dir), false);
});

test("bridge retains the last protocol response before trailing stdout", async (t) => {
	const noiseDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-trailing-"));
	const previousPythonPath = process.env.PYTHONPATH;
	t.after(() => {
		delete process.env.PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH;
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		fs.rmSync(noiseDir, { recursive: true, force: true });
	});
	fs.writeFileSync(
		path.join(noiseDir, "sitecustomize.py"),
		"import atexit\natexit.register(lambda: print('late-noise'))\n",
		"utf8",
	);
	process.env.PYTHONPATH = [noiseDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH = "1";
	const sessionId = randomBytes(6).toString("hex");
	const result = await runPython("kill", { session_id: sessionId });

	assert.equal(result.ok, false);
	assert.equal(result.data.protocol_error, "exit_status_mismatch");
	assert.equal(result.data.session_id, sessionId);
	assert.equal(result.data.backend?.already_absent, true);
});

test("kill rejects a success body paired with a failing backend exit", async (t) => {
	const runtime = createRuntime();
	let session;
	t.after(async () => {
		delete process.env.PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH;
		if (!session || !fs.existsSync(session.tmp_dir)) return;
		await runtime.tool("silent_kill").execute(
			"exit-mismatch-fallback",
			{ session_id: session.session_id },
			new AbortController().signal,
		);
	});

	session = body(
		await runtime.tool("silent_spawn").execute(
			"exit-mismatch-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	process.env.PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH = "1";
	await assert.rejects(
		runtime.tool("silent_kill").execute(
			"exit-mismatch-kill",
			{ session_id: session.session_id },
			new AbortController().signal,
		),
		(error) =>
			toolError(error).error === "backend exit status and kill response disagree",
	);
	delete process.env.PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH;

	const retried = body(
		await runtime.tool("silent_kill").execute(
			"exit-mismatch-retry",
			{ session_id: session.session_id },
			new AbortController().signal,
		),
	);
	assert.equal(retried.already_absent, undefined);
	assert.equal(retried.ok, true);
});

test("spawn cleans a live session when backend exit and JSON success disagree", async (t) => {
	const runtime = createRuntime();
	t.after(async () => {
		delete process.env.PI_SILENT_GUI_TEST_SPAWN_EXIT_MISMATCH;
		await runtime.shutdown();
	});
	process.env.PI_SILENT_GUI_TEST_SPAWN_EXIT_MISMATCH = "1";
	let failure;
	await assert.rejects(
		runtime.tool("silent_spawn").execute(
			"spawn-exit-mismatch",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
		(error) => {
			failure = toolError(error);
			return failure.error === "backend exit status and spawn response disagree";
		},
	);
	delete process.env.PI_SILENT_GUI_TEST_SPAWN_EXIT_MISMATCH;

	assert.match(failure.session_id, /^[0-9a-f]{12}$/);
	assert.equal(failure.protocol_error, "exit_status_mismatch");
	assert.equal(failure.cleanup?.ok, true);
	assert.equal(failure.orphan_cleanup_registered, false);
	assert.equal(fs.existsSync(failure.backend.tmp_dir), false);
});

test("extension lifecycle, bounded output, orphan and abort cleanup", async (t) => {
	const pendingSessions = new Set();
	const noiseDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-output-"));
	const previousPythonPath = process.env.PYTHONPATH;
	const trackSession = (data) => {
		if (
			/^[0-9a-f]{12}$/.test(data?.session_id || "") &&
			(data.registered === true || data.orphan_cleanup_registered === true)
		) {
			pendingSessions.add(data.session_id);
		}
		return data;
	};

	t.after(async () => {
		delete process.env.PI_SILENT_GUI_TEST_NOISE;
		delete process.env.PI_SILENT_GUI_TEST_ORPHAN_CLEANUP;
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		const cleanupErrors = [];
		for (const sessionId of pendingSessions) {
			try {
				await kill.execute(
					"cleanup",
					{ session_id: sessionId },
					new AbortController().signal,
				);
			} catch (error) {
				cleanupErrors.push(error);
			}
		}
		fs.rmSync(noiseDir, { recursive: true, force: true });
		if (cleanupErrors.length) throw new AggregateError(cleanupErrors, "session cleanup failed");
	});

	fs.writeFileSync(
		path.join(noiseDir, "sitecustomize.py"),
		[
			"import os, sys",
			"size = int(os.environ.get('PI_SILENT_GUI_TEST_NOISE', '0'))",
			"if size:",
			"    sys.stdout.buffer.write((b'\\xe6\\xb5\\x8b' * ((size + 2) // 3))[:size] + b'\\n')",
			"    sys.stdout.buffer.flush()",
			"    sys.stderr.buffer.write(b'e' * size)",
			"    sys.stderr.buffer.flush()",
		].join("\n"),
		"utf8",
	);
	process.env.PYTHONPATH = [noiseDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_TEST_NOISE = String(1024 * 1024 + 64 * 1024);
	let active;
	try {
		active = trackSession(
			body(
				await spawn.execute(
					"spawn",
					{ exe: TEST_GUI_EXE },
					new AbortController().signal,
					undefined,
					{ cwd: noiseDir },
				),
			),
		);
	} catch (error) {
		trackSession(toolError(error));
		throw error;
	} finally {
		delete process.env.PI_SILENT_GUI_TEST_NOISE;
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
	}
	assert.equal(typeof active.session_id, "string");
	assert.ok(path.isAbsolute(active.tmp_dir));

	const waited = body(
		await wait.execute(
			"wait",
			{ session_id: active.session_id, timeout_ms: 5000 },
			new AbortController().signal,
		),
	);
	assert.equal(waited.alive, true);
	assert.equal(waited.exit_code, null);
	assert.ok(waited.windows.some((item) => item.hwnd === waited.hwnd));

	const captureResult = await capture.execute(
		"capture",
		{ session_id: active.session_id, out_path: "capture.png" },
		new AbortController().signal,
		undefined,
		{ cwd: noiseDir },
	);
	const captured = body(captureResult);
	assert.equal(typeof captured.all_black, "boolean");
	assert.ok(path.isAbsolute(captured.path));
	assert.ok(captured.client);
	assert.deepEqual(pngSize(captured.path), [captured.width, captured.height]);
	assert.equal(captureResult.content.length, 2);
	assert.equal(captureResult.content[1].type, "image");
	assert.equal(captureResult.content[1].mimeType, "image/png");
	assert.deepEqual(
		Buffer.from(captureResult.content[1].data, "base64"),
		fs.readFileSync(captured.path),
	);

	const client = captured.client;
	const clicked = body(
		await click.execute(
			"click",
			{
				session_id: active.session_id,
				x: client.x + Math.floor(client.width / 2),
				y: client.y + Math.floor(client.height / 2),
			},
			new AbortController().signal,
		),
	);
	assert.equal(clicked.hwnd, captured.hwnd);

	await typeText.execute(
		"type",
		{ session_id: active.session_id, text: "hi" },
		new AbortController().signal,
	);
	const keyed = body(
		await key.execute(
			"key",
			{ session_id: active.session_id, key: "return", count: 2, interval_ms: 50 },
			new AbortController().signal,
		),
	);
	assert.equal(keyed.count, 2);

	fs.writeFileSync(
		path.join(active.tmp_dir, "session.json"),
		JSON.stringify({
			status: "ready",
			session_id: active.session_id,
			job_name: `pi_silent_job_${active.session_id}_${"0".repeat(32)}`,
			desktop: `pi_silent_${active.session_id}_${"0".repeat(32)}`,
			broker_pid: "broken",
			broker_created: {},
			pid: [],
			root_created: "broken",
		}),
	);
	const killed = body(
		await kill.execute("kill", { session_id: active.session_id }, new AbortController().signal),
	);
	assert.equal(killed.ok, true);
	assert.equal(fs.existsSync(active.tmp_dir), false);
	pendingSessions.delete(active.session_id);

	const rawCleanupToken = "c".repeat(64);
	const orphanResult = await runPython(
		"spawn",
		{ exe: TEST_GUI_EXE, cwd: noiseDir, cleanup_token: rawCleanupToken },
		undefined,
		false,
	);
	assert.equal(orphanResult.ok, true, JSON.stringify(orphanResult.data));
	const orphan = orphanResult.data;
	pendingSessions.add(orphan.session_id);
	t.after(async () => {
		if (!pendingSessions.has(orphan.session_id)) return;
		const fallback = await runPython("kill", {
			session_id: orphan.session_id,
			_job_name: orphan.job_name,
			_broker_pid: orphan.broker_pid,
			_broker_created: orphan.broker_created,
			_root_pid: orphan.pid,
			_root_created: orphan.root_created,
			cleanup_token: rawCleanupToken,
		});
		if (!fallback.ok) throw new Error(`orphan fallback cleanup failed: ${fallback.raw}`);
		pendingSessions.delete(orphan.session_id);
	});
	const orphanKillResult = await runPython("kill", {
		session_id: orphan.session_id,
		cleanup_token: rawCleanupToken,
	});
	assert.equal(orphanKillResult.ok, true, orphanKillResult.raw);
	const orphanKilled = orphanKillResult.data;
	assert.equal(orphanKilled.ok, true);
	assert.equal(fs.existsSync(orphan.tmp_dir), false);
	pendingSessions.delete(orphan.session_id);

	process.env.PI_SILENT_GUI_TEST_ORPHAN_CLEANUP = "1";
	let orphanHandoff;
	try {
		const unexpected = trackSession(
			body(
				await spawn.execute(
					"orphan-handoff",
					{ exe: TEST_GUI_EXE },
					new AbortController().signal,
				),
			),
		);
		assert.fail(`orphan cleanup handoff unexpectedly succeeded: ${JSON.stringify(unexpected)}`);
	} catch (error) {
		orphanHandoff = trackSession(toolError(error));
	} finally {
		delete process.env.PI_SILENT_GUI_TEST_ORPHAN_CLEANUP;
	}
	assert.equal(orphanHandoff.orphan_cleanup_required, true);
	assert.equal(orphanHandoff.cleanup?.ok, true);
	assert.equal(orphanHandoff.orphan_cleanup_registered, false);
	assert.equal(fs.existsSync(orphanHandoff.tmp_dir), false);

	const abort = new AbortController();
	setTimeout(() => abort.abort(), 100);
	let aborted;
	try {
		const unexpected = trackSession(
			body(
				await spawn.execute(
					"abort",
					{ exe: "ping.exe", args: ["-t", "127.0.0.1"] },
					abort.signal,
				),
			),
		);
		assert.fail(`aborted spawn unexpectedly succeeded: ${JSON.stringify(unexpected)}`);
	} catch (error) {
		aborted = trackSession(toolError(error));
	}
	assert.equal(aborted.cleanup?.ok, true);
});

test("input tools reject missing fields", async (t) => {
	const runtime = createRuntime();
	let session;
	t.after(async () => {
		if (!session || !fs.existsSync(session.tmp_dir)) return;
		await runtime.tool("silent_kill").execute(
			"input-entry-cleanup",
			{ session_id: session.session_id },
			new AbortController().signal,
		);
	});

	session = body(
		await runtime.tool("silent_spawn").execute(
			"input-entry-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);

	const sid = session.session_id;
	await assert.rejects(
		runtime.tool("silent_type").execute(
			"empty-type",
			{ session_id: sid, text: "" },
			new AbortController().signal,
		),
		(error) => toolError(error).error === "type requires text",
	);
	await assert.rejects(
		runtime.tool("silent_key").execute(
			"empty-key",
			{ session_id: sid, key: "" },
			new AbortController().signal,
		),
		(error) => toolError(error).error === "key required",
	);

	const keyed = body(
		await runtime.tool("silent_key").execute(
			"key-ok",
			{ session_id: sid, key: "return" },
			new AbortController().signal,
		),
	);
	assert.equal(keyed.key, "return");
});

test("session serializes message/capture and rejects work after kill", async (t) => {
	const runtime = createRuntime();
	let session;
	t.after(async () => {
		if (!session || !fs.existsSync(session.tmp_dir)) return;
		await runtime.tool("silent_kill").execute(
			"serial-cleanup",
			{ session_id: session.session_id },
			new AbortController().signal,
		);
	});

	session = body(
		await runtime.tool("silent_spawn").execute(
			"serial-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);

	const outA = path.join(session.tmp_dir, "serial-a.png");
	const outB = path.join(session.tmp_dir, "serial-b.png");
	const [capturedA, capturedB, keyed] = await Promise.all([
		runtime.tool("silent_capture").execute(
			"serial-capture-a",
			{ session_id: session.session_id, out_path: outA },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
		runtime.tool("silent_capture").execute(
			"serial-capture-b",
			{ session_id: session.session_id, out_path: outB },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
		runtime.tool("silent_key").execute(
			"serial-key",
			{ session_id: session.session_id, key: "return" },
			new AbortController().signal,
		),
	]);
	assert.equal(body(capturedA).session_id, session.session_id);
	assert.equal(body(capturedB).session_id, session.session_id);
	assert.equal(body(keyed).key, "return");
	assert.equal(fs.existsSync(outA), true);
	assert.equal(fs.existsSync(outB), true);

	const killed = body(
		await runtime.tool("silent_kill").execute(
			"serial-kill",
			{ session_id: session.session_id },
			new AbortController().signal,
		),
	);
	assert.equal(killed.ok, true);

	await assert.rejects(
		runtime.tool("silent_key").execute(
			"after-kill-message",
			{ session_id: session.session_id, key: "return" },
			new AbortController().signal,
		),
		(error) => toolError(error).error.includes(`unknown session_id: ${session.session_id}`),
	);
	await assert.rejects(
		runtime.tool("silent_capture").execute(
			"after-kill-capture",
			{ session_id: session.session_id },
			new AbortController().signal,
		),
		(error) => toolError(error).error.includes(`unknown session_id: ${session.session_id}`),
	);
});

test("kill aborts an active capture without waiting for its helper", async (t) => {
	const runtime = createRuntime();
	const hangDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-kill-active-"));
	const marker = path.join(hangDir, "capture-entered.txt");
	const previousPythonPath = process.env.PYTHONPATH;
	const previousMarker = process.env.PI_SILENT_GUI_ACTIVE_MARKER;
	let session;

	t.after(async () => {
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		if (previousMarker === undefined) delete process.env.PI_SILENT_GUI_ACTIVE_MARKER;
		else process.env.PI_SILENT_GUI_ACTIVE_MARKER = previousMarker;
		if (session && fs.existsSync(session.tmp_dir)) {
			await runtime.tool("silent_kill").execute(
				"kill-active-cleanup",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(hangDir, { recursive: true, force: true });
	});

	fs.writeFileSync(
		path.join(hangDir, "sitecustomize.py"),
		[
			"import os, pathlib, sys, time",
			"if len(sys.argv) > 1 and sys.argv[1] == 'capture':",
			"    pathlib.Path(os.environ['PI_SILENT_GUI_ACTIVE_MARKER']).write_text('entered', encoding='utf-8')",
			"    time.sleep(30)",
		].join("\n"),
		"utf8",
	);

	session = body(
		await runtime.tool("silent_spawn").execute(
			"kill-active-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	process.env.PYTHONPATH = [hangDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_ACTIVE_MARKER = marker;

	const captureOutcome = runtime.tool("silent_capture").execute(
		"kill-active-capture",
		{ session_id: session.session_id },
		new AbortController().signal,
		undefined,
		{ cwd: process.cwd() },
	).then(
		(value) => ({ ok: true, value }),
		(error) => ({ ok: false, error }),
	);
	const queuedOutcome = runtime.tool("silent_key").execute(
		"kill-queued-message",
		{ session_id: session.session_id, key: "return" },
		new AbortController().signal,
	).then(
		(value) => ({ ok: true, value }),
		(error) => ({ ok: false, error }),
	);
	const markerDeadline = Date.now() + 5000;
	while (!fs.existsSync(marker) && Date.now() < markerDeadline) {
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
	assert.equal(fs.existsSync(marker), true, "capture helper did not enter the injected hang");

	const started = Date.now();
	const killResult = body(
		await runtime.tool("silent_kill").execute(
			"kill-active",
			{ session_id: session.session_id },
			new AbortController().signal,
		),
	);
	const [captureResult, queuedResult] = await Promise.all([captureOutcome, queuedOutcome]);
	assert.equal(captureResult.ok, false, "active capture unexpectedly succeeded during kill");
	assert.equal(queuedResult.ok, false, "queued message unexpectedly survived session close");
	assert.ok(Date.now() - started < 10_000, "kill waited for active or queued work");
	assert.equal(killResult.ok, true);
	assert.equal(fs.existsSync(session.tmp_dir), false);
});

test("concurrent kill callers observe independent abort signals", async (t) => {
	const runtime = createRuntime();
	const delayDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-kill-callers-"));
	const previousPythonPath = process.env.PYTHONPATH;
	let session;

	t.after(async () => {
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		if (session && fs.existsSync(session.tmp_dir)) {
			await runtime.tool("silent_kill").execute(
				"kill-callers-cleanup",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(delayDir, { recursive: true, force: true });
	});

	fs.writeFileSync(
		path.join(delayDir, "sitecustomize.py"),
		[
			"import sys, time",
			"if len(sys.argv) > 1 and sys.argv[1] == 'kill':",
			"    time.sleep(0.75)",
		].join("\n"),
		"utf8",
	);
	session = body(
		await runtime.tool("silent_spawn").execute(
			"kill-callers-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	process.env.PYTHONPATH = [delayDir, previousPythonPath].filter(Boolean).join(path.delimiter);

	const alreadyAborted = new AbortController();
	alreadyAborted.abort(new Error("caller never admitted"));
	await assert.rejects(
		runtime.tool("silent_kill").execute(
			"kill-callers-pre-aborted",
			{ session_id: session.session_id },
			alreadyAborted.signal,
		),
	);

	const firstAbort = new AbortController();
	const first = runtime.tool("silent_kill").execute(
		"kill-callers-first",
		{ session_id: session.session_id },
		firstAbort.signal,
	).then(
		(value) => ({ ok: true, value }),
		(error) => ({ ok: false, error }),
	);
	const second = runtime.tool("silent_kill").execute(
		"kill-callers-second",
		{ session_id: session.session_id },
		new AbortController().signal,
	);
	setTimeout(() => firstAbort.abort(new Error("first caller left")), 100);

	const firstResult = await first;
	assert.equal(firstResult.ok, false);
	const secondResult = body(await second);
	assert.equal(secondResult.ok, true);
	assert.equal(fs.existsSync(session.tmp_dir), false);
});

test("overwrite captures serialize the same normalized path across sessions", async (t) => {
	const runtime = createRuntime();
	const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-cross-session-"));
	const delayDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-cross-delay-"));
	const activeMarker = path.join(workDir, "capture.active");
	const collisionMarker = path.join(workDir, "capture.collision");
	const previousPythonPath = process.env.PYTHONPATH;
	const sessions = [];

	t.after(async () => {
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		for (const session of sessions) {
			if (!fs.existsSync(session.tmp_dir)) continue;
			await runtime.tool("silent_kill").execute(
				"cross-session-cleanup",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(delayDir, { recursive: true, force: true });
		fs.rmSync(workDir, { recursive: true, force: true });
	});

	fs.writeFileSync(
		path.join(delayDir, "sitecustomize.py"),
		[
			"import os, pathlib, sys, time",
			"if len(sys.argv) > 1 and sys.argv[1] == 'capture':",
			"    active = pathlib.Path(os.environ['PI_SILENT_GUI_CAPTURE_ACTIVE'])",
			"    collision = pathlib.Path(os.environ['PI_SILENT_GUI_CAPTURE_COLLISION'])",
			"    owned = False",
			"    try:",
			"        fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY)",
			"        os.close(fd)",
			"        owned = True",
			"    except FileExistsError:",
			"        collision.write_text('overlap', encoding='utf-8')",
			"    time.sleep(0.6)",
			"    if owned:",
			"        active.unlink(missing_ok=True)",
		].join("\n"),
		"utf8",
	);
	for (const id of ["cross-session-a", "cross-session-b"]) {
		sessions.push(
			body(
				await runtime.tool("silent_spawn").execute(
					id,
					{ exe: TEST_GUI_EXE },
					new AbortController().signal,
					undefined,
					{ cwd: workDir },
				),
			),
		);
	}
	process.env.PYTHONPATH = [delayDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_CAPTURE_ACTIVE = activeMarker;
	process.env.PI_SILENT_GUI_CAPTURE_COLLISION = collisionMarker;
	t.after(() => {
		delete process.env.PI_SILENT_GUI_CAPTURE_ACTIVE;
		delete process.env.PI_SILENT_GUI_CAPTURE_COLLISION;
	});

	const output = path.join(workDir, "shared.png");
	const started = Date.now();
	const results = await Promise.all([
		runtime.tool("silent_capture").execute(
			"cross-session-capture-a",
			{ session_id: sessions[0].session_id, out_path: "shared.png", overwrite: true },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
		runtime.tool("silent_capture").execute(
			"cross-session-capture-b",
			{ session_id: sessions[1].session_id, out_path: path.join(workDir, ".", "shared.png"), overwrite: true },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
	]);
	assert.ok(Date.now() - started >= 1000, "same-path captures overlapped");
	assert.equal(fs.existsSync(collisionMarker), false);
	assert.equal(body(results[0]).path, output);
	assert.equal(body(results[1]).path, output);
	assert.equal(fs.existsSync(output), true);
});

test("capture overwrite defaults to reject and allows an explicit overwrite", async (t) => {
	const runtime = createRuntime();
	const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-overwrite-"));
	let session;
	t.after(async () => {
		if (session && fs.existsSync(session.tmp_dir)) {
			await runtime.tool("silent_kill").execute(
				"overwrite-cleanup",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(workDir, { recursive: true, force: true });
	});

	session = body(
		await runtime.tool("silent_spawn").execute(
			"overwrite-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
	);

	const first = body(
		await runtime.tool("silent_capture").execute(
			"overwrite-first",
			{ session_id: session.session_id, out_path: "shot.png" },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
	);
	assert.equal(fs.existsSync(first.path), true);

	await assert.rejects(
		runtime.tool("silent_capture").execute(
			"overwrite-default",
			{ session_id: session.session_id, out_path: "shot.png" },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
		(error) => /output already exists/i.test(String(toolError(error).error || error.message)),
	);

	const replaced = body(
		await runtime.tool("silent_capture").execute(
			"overwrite-allow",
			{ session_id: session.session_id, out_path: "shot.png", overwrite: true },
			new AbortController().signal,
			undefined,
			{ cwd: workDir },
		),
	);
	assert.equal(replaced.path, first.path);
	assert.equal(fs.existsSync(replaced.path), true);
});

test("capture hard timeout removes its caller-owned pending file", async (t) => {
	const runtime = createRuntime();
	const hangDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-silent-gui-capture-timeout-"));
	const previousPythonPath = process.env.PYTHONPATH;
	const previousMarker = process.env.PI_SILENT_GUI_PENDING_MARKER;
	const originalTimeout = AbortSignal.timeout;
	const marker = path.join(hangDir, "pending-created.txt");
	const output = path.join(hangDir, "timeout.png");
	let session;

	t.after(async () => {
		AbortSignal.timeout = originalTimeout;
		if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
		else process.env.PYTHONPATH = previousPythonPath;
		if (previousMarker === undefined) delete process.env.PI_SILENT_GUI_PENDING_MARKER;
		else process.env.PI_SILENT_GUI_PENDING_MARKER = previousMarker;
		if (session && fs.existsSync(session.tmp_dir)) {
			await runtime.tool("silent_kill").execute(
				"capture-timeout-cleanup",
				{ session_id: session.session_id },
				new AbortController().signal,
			);
		}
		fs.rmSync(hangDir, { recursive: true, force: true });
	});

	fs.writeFileSync(
		path.join(hangDir, "sitecustomize.py"),
		[
			"import os, pathlib, sys, time",
			"if len(sys.argv) > 1 and sys.argv[1] == 'capture':",
			"    original_open = pathlib.Path.open",
			"    def delayed_open(self, *args, **kwargs):",
			"        stream = original_open(self, *args, **kwargs)",
			"        if self.name.startswith('.timeout.png.') and self.name.endswith('.tmp'):",
			"            pathlib.Path(os.environ['PI_SILENT_GUI_PENDING_MARKER']).write_text(str(self), encoding='utf-8')",
			"            time.sleep(30)",
			"        return stream",
			"    pathlib.Path.open = delayed_open",
		].join("\n"),
		"utf8",
	);

	session = body(
		await runtime.tool("silent_spawn").execute(
			"capture-timeout-spawn",
			{ exe: TEST_GUI_EXE },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);

	process.env.PYTHONPATH = [hangDir, previousPythonPath].filter(Boolean).join(path.delimiter);
	process.env.PI_SILENT_GUI_PENDING_MARKER = marker;
	// Shrink only the capture hard timeout while leaving enough time to create the temp file.
	AbortSignal.timeout = (ms) => originalTimeout(ms >= 15_000 ? 1000 : ms);

	const started = Date.now();
	await assert.rejects(
		runtime.tool("silent_capture").execute(
			"capture-timeout",
			{ session_id: session.session_id, out_path: output, overwrite: true },
			new AbortController().signal,
			undefined,
			{ cwd: process.cwd() },
		),
	);
	const elapsed = Date.now() - started;
	assert.ok(elapsed < 5000, `capture hard timeout took ${elapsed}ms`);
	assert.equal(fs.existsSync(marker), true, "capture did not create its pending file");
	const cleanupDeadline = Date.now() + 5000;
	while (
		fs.readdirSync(hangDir).some((name) => name.startsWith(".timeout.png.")) &&
		Date.now() < cleanupDeadline
	) {
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
	assert.deepEqual(
		fs.readdirSync(hangDir).filter((name) => name.startsWith(".timeout.png.")),
		[],
	);
});

/** Validate backend responses enough to keep a session operable. */
import fs from "node:fs";
import path from "node:path";

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export type SessionRec = {
	pid: number;
	rootCreated: string;
	desktop: string;
	exe: string;
	jobName: string;
	brokerPid: number;
	brokerCreated: string;
	tmpDir: string;
	targetElevated?: boolean;
	// "inject" once a payload armed this session, else "message". Drives which
	// dispatch the backend uses and what the model is told it can do.
	inputMode: string;
	startedAt: number;
};

function positiveInt(value: unknown): number | undefined {
	return typeof value === "number" && Number.isSafeInteger(value) && value > 0
		? value
		: undefined;
}

function decimalIdentity(value: unknown): string | undefined {
	return typeof value === "string" && /^[1-9]\d*$/.test(value) ? value : undefined;
}

export function stringValue(value: unknown): string | undefined {
	return typeof value === "string" && value.length > 0 ? value : undefined;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === "object" && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: undefined;
}

function safeInteger(value: unknown): value is number {
	return typeof value === "number" && Number.isSafeInteger(value);
}

function validDesktopName(value: string, sessionId: string): boolean {
	return new RegExp(`^pi_silent_${sessionId}_[0-9a-f]{32}$`).test(value);
}

function validJobName(value: string, sessionId: string): boolean {
	return new RegExp(`^pi_silent_job_${sessionId}_[0-9a-f]{32}$`).test(value);
}

function validSessionTempDir(value: string, sessionId: string): boolean {
	const localAppData = process.env.LOCALAPPDATA;
	if (!localAppData) return false;
	const expected = path.resolve(localAppData, "Temp", "pi-silent-gui", sessionId);
	return path.normalize(value).toLowerCase() === path.normalize(expected).toLowerCase();
}

function recFrom(data: Record<string, unknown>, exe: string): SessionRec {
	return {
		pid: positiveInt(data.pid) ?? 0,
		rootCreated: decimalIdentity(data.root_created) ?? "",
		desktop: stringValue(data.desktop) ?? "",
		exe,
		jobName: stringValue(data.job_name) ?? "",
		brokerPid: positiveInt(data.broker_pid) ?? 0,
		brokerCreated: decimalIdentity(data.broker_created) ?? "",
		tmpDir: stringValue(data.tmp_dir) ?? "",
		targetElevated:
			typeof data.target_elevated === "boolean" ? data.target_elevated : undefined,
		inputMode: data.input_mode === "inject" ? "inject" : "message",
		startedAt: Date.now(),
	};
}

export function cleanupRecordFromFailedSpawn(
	data: Record<string, unknown>,
	exe: string,
): { sessionId: string; rec: SessionRec } | undefined {
	const sessionId = stringValue(data.session_id);
	if (!sessionId || !/^[0-9a-f]{12}$/.test(sessionId)) return undefined;
	const rec = recFrom(data, exe);
	return validJobName(rec.jobName, sessionId) && rec.brokerPid && rec.brokerCreated
		? { sessionId, rec }
		: undefined;
}

export function validateSpawn(data: Record<string, unknown>): {
	sessionId: string;
	rec: SessionRec;
} {
	const sessionId = stringValue(data.session_id);
	if (!sessionId || !/^[0-9a-f]{12}$/.test(sessionId)) throw new Error("invalid spawn session_id");
	const rec = recFrom(data, stringValue(data.exe) ?? "");
	if (
		!rec.pid ||
		!rec.rootCreated ||
		!validDesktopName(rec.desktop, sessionId) ||
		!validJobName(rec.jobName, sessionId) ||
		!rec.brokerPid ||
		!rec.brokerCreated ||
		!validSessionTempDir(rec.tmpDir, sessionId)
	) {
		throw new Error("invalid spawn response identity/namespace");
	}
	return { sessionId, rec };
}

function validateSessionId(data: Record<string, unknown>, sessionId: string) {
	if (data.session_id !== sessionId) throw new Error("backend session_id mismatch");
}

function responseHwnd(
	data: Record<string, unknown>,
	sessionId: string,
	requestedHwnd?: unknown,
): number {
	validateSessionId(data, sessionId);
	const hwnd = positiveInt(data.hwnd);
	if (!hwnd) throw new Error("invalid hwnd");
	const expected = positiveInt(requestedHwnd);
	if (requestedHwnd !== undefined && (!expected || expected !== hwnd)) {
		throw new Error("hwnd mismatch");
	}
	return hwnd;
}

function validateSnapshot(data: Record<string, unknown>) {
	if (typeof data.alive !== "boolean") throw new Error("invalid session snapshot");
	if (data.exit_code !== null && data.exit_code !== undefined && !safeInteger(data.exit_code)) {
		throw new Error("invalid session snapshot");
	}
	if (!Array.isArray(data.windows)) throw new Error("invalid session snapshot");
}

export function validateWait(data: Record<string, unknown>, sessionId: string) {
	responseHwnd(data, sessionId);
	validateSnapshot(data);
}

export function validateMessage(
	data: Record<string, unknown>,
	sessionId: string,
	request: Record<string, unknown>,
) {
	responseHwnd(data, sessionId, request.hwnd ?? request.expected_hwnd);
	if (request.action === "click") {
		if (!safeInteger(request.x) || !safeInteger(request.y)) {
			throw new Error("click requires integer x,y");
		}
		const window = objectValue(data.window);
		const width = positiveInt(window?.width) ?? positiveInt(data.width);
		const height = positiveInt(window?.height) ?? positiveInt(data.height);
		if (
			width &&
			height &&
			(request.x < 0 || request.y < 0 || request.x >= width || request.y >= height)
		) {
			throw new Error("click outside window");
		}
	}
	if (request.action === "click" || request.action === "key") {
		const expected = positiveInt(request.count) ?? 1;
		const actual = data.count === undefined ? 1 : positiveInt(data.count);
		if (actual !== expected) throw new Error("invalid repeat count");
	}
	if (request.action === "type" && !positiveInt(data.chars)) {
		throw new Error("invalid type response");
	}
}

export function validateCapture(
	data: Record<string, unknown>,
	sessionId: string,
	requestedPath?: string,
) {
	validateSessionId(data, sessionId);
	const capturePath = stringValue(data.path);
	const topHwnd = positiveInt(data.hwnd);
	const window = objectValue(data.window);
	const width = positiveInt(data.width) ?? positiveInt(window?.width);
	const height = positiveInt(data.height) ?? positiveInt(window?.height);
	if (!topHwnd || !capturePath || !path.isAbsolute(capturePath) || !width || !height) {
		throw new Error("invalid capture response");
	}
	if (typeof data.all_black !== "boolean") {
		throw new Error("invalid capture response");
	}
	const header = Buffer.alloc(24);
	const captureFd = fs.openSync(capturePath, "r");
	let requestedFd: number | undefined;
	try {
		const captureStat = fs.fstatSync(captureFd, { bigint: true });
		if (!captureStat.isFile() || captureStat.size <= 0n) {
			throw new Error("capture path is not a non-empty file");
		}
		if (requestedPath) {
			requestedFd = fs.openSync(requestedPath, "r");
			const requestedStat = fs.fstatSync(requestedFd, { bigint: true });
			if (captureStat.dev !== requestedStat.dev || captureStat.ino !== requestedStat.ino) {
				throw new Error("capture path does not match requested out_path");
			}
		}
		if (fs.readSync(captureFd, header, 0, header.length, 0) !== header.length) {
			throw new Error("capture PNG header is incomplete");
		}
	} finally {
		if (requestedFd !== undefined) fs.closeSync(requestedFd);
		fs.closeSync(captureFd);
	}
	if (
		!header.subarray(0, 8).equals(PNG_MAGIC) ||
		header.readUInt32BE(8) !== 13 ||
		header.toString("ascii", 12, 16) !== "IHDR"
	) {
		throw new Error("capture file is not a PNG with IHDR");
	}
	const pngWidth = header.readUInt32BE(16);
	const pngHeight = header.readUInt32BE(20);
	if (!pngWidth || !pngHeight || pngWidth !== width || pngHeight !== height) {
		throw new Error("capture PNG dimensions do not match response window");
	}
}

export function validateKill(data: Record<string, unknown>, sessionId: string) {
	validateSessionId(data, sessionId);
	if (typeof data.ok !== "boolean") throw new Error("invalid kill response");
}

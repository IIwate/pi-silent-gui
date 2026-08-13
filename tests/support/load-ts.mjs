import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const root = fileURLToPath(new URL("../../", import.meta.url));
export const manifest = JSON.parse(
	fs.readFileSync(path.join(root, "package.json"), "utf8"),
);

const npmRoot = execFileSync(
	process.env.ComSpec || "cmd.exe",
	["/d", "/s", "/c", "npm root -g"],
	{ encoding: "utf8" },
).trim();
const piRoot = path.join(npmRoot, "@earendil-works", "pi-coding-agent");
const require = createRequire(import.meta.url);
process.env.NODE_PATH = [path.join(piRoot, "node_modules"), npmRoot, process.env.NODE_PATH]
	.filter(Boolean)
	.join(path.delimiter);
require("node:module")._initPaths();
const { createJiti } = require(require.resolve("jiti", { paths: [piRoot] }));
const jiti = createJiti(path.join(piRoot, "dist", "cli.js"), { interopDefault: true });

export function loadTs(relativePath) {
	return jiti.import(path.resolve(root, relativePath));
}

export function loadPiEntry() {
	const entries = manifest.pi?.extensions;
	if (!Array.isArray(entries) || entries.length !== 1 || typeof entries[0] !== "string") {
		throw new Error("package manifest must declare exactly one Pi extension");
	}
	return loadTs(entries[0]);
}

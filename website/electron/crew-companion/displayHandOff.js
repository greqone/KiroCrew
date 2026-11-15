/**
 * displayHandOff.js: hand the built-in avatar's display to the desktop window host.
 *
 * The built-in remembers which display its avatar lives on in its own userData
 * file (`crew-companion-pet-position.json`, written by petOverlay.js). Once the
 * app's own repository copy is installed, the avatar is that copy's spanning
 * overlay, which the window host opens on the display it remembers in
 * `app-window-displays.json` under `<app>/<window id>`. With nothing there the host
 * elects the display under the cursor, so a multi-display user would find the
 * avatar on another screen the first time the copy runs.
 *
 * Copying the remembered display across, once and only while the host has none
 * for that window, keeps it where it was. The key names the window id the app's
 * repository declares for its avatar (`pet`); the host reads it only for an
 * installed app of this name declaring a spanning window of that id, so writing
 * it ahead of time changes nothing for anyone else. Called by main.js before the
 * host starts, because the host reads its file once, on first use.
 */

const fs = require("fs");
const path = require("path");

const BUILTIN_POSITION_FILE = "crew-companion-pet-position.json";
const HOST_KEY = "crew-companion/pet";

function readJsonObject(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf-8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Seed the host's remembered display for the avatar from the built-in's own.
 *
 * @param {string} userDataDir the shell's userData directory, where both files live
 * @param {string} hostMemoryFile the host's `app-window-displays.json`
 * @returns {boolean} true when a display was handed over
 */
function seedHostDisplayMemory(userDataDir, hostMemoryFile) {
  if (!userDataDir || !hostMemoryFile) return false;
  const builtin = readJsonObject(path.join(userDataDir, BUILTIN_POSITION_FILE));
  const displayId = builtin ? builtin.displayId : undefined;
  if (typeof displayId !== "number" || !Number.isFinite(displayId)) return false;
  let host = {};
  if (fs.existsSync(hostMemoryFile)) {
    host = readJsonObject(hostMemoryFile);
    // An unreadable host file is the host's to recover; never overwrite it.
    if (!host) return false;
  }
  if (Object.prototype.hasOwnProperty.call(host, HOST_KEY)) return false;
  try {
    fs.writeFileSync(hostMemoryFile, JSON.stringify({ ...host, [HOST_KEY]: displayId }), { mode: 0o600 });
    return true;
  } catch {
    // The avatar then opens on the display under the cursor; nothing else changes.
    return false;
  }
}

module.exports = { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY };

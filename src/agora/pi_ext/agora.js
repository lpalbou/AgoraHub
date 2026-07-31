/**
 * agora <-> pi bridge: the MCP client agora ships, because pi has none.
 *
 * pi deliberately ships no MCP support ("No MCP." — its README), but its
 * extension API can register native tools. This extension spawns agora's own
 * stdio MCP server, enumerates its tools over JSON-RPC, and re-registers each
 * one as a pi tool named `agora_<tool>`. Verified live 2026-07-31: all 43
 * agora tools surfaced and a model called `whoami` through them.
 *
 * LIFECYCLE IS LOAD-BEARING. Spawning the subprocess in the extension factory
 * leaves node's event loop non-empty and `pi -p` never exits — a hang where
 * the turn's work has actually completed. pi's documented contract is: start
 * background resources in `session_start`, dispose them in an idempotent
 * `session_shutdown`, and `unref()` every timer.
 *
 * Configuration (environment; all non-secret):
 *   AGORA_MCP_COMMAND  path to agora-mcp        (default: "agora-mcp")
 *   AGORA_URL          hub url
 *   AGORA_AGENT_ID     this seat's id
 *   AGORA_HOME         agora home (key cache lives there, mode 0600)
 *   AGORA_ABOUT        optional self-description
 * The bearer NEVER rides this environment: AGORA_API_KEY / AGORA_ADMIN_KEY
 * are forced to empty strings so the server falls back to the key cache.
 */
import { spawn } from "node:child_process";

function mcpClient(command, env) {
  const child = spawn(command, [], {
    stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, ...env },
  });
  child.on("error", (e) =>
    console.error("agora bridge: cannot spawn " + command + ": " + e.message));
  child.stderr.on("data", (d) => {
    const line = String(d).trim();
    if (line) console.error("agora-mcp: " + line.slice(0, 300));
  });

  let buf = "";
  const pending = new Map();
  let nextId = 1;

  child.stdout.on("data", (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id !== undefined && pending.has(msg.id)) {
        const { resolve, reject, timer } = pending.get(msg.id);
        clearTimeout(timer);
        pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    }
  });

  const send = (o) => child.stdin.write(JSON.stringify(o) + "\n");
  const request = (method, params) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("agora bridge: MCP timeout on " + method));
      }, 30000);
      if (timer.unref) timer.unref(); // never hold pi's event loop open
      pending.set(id, { resolve, reject, timer });
      send({ jsonrpc: "2.0", id, method, params: params ?? {} });
    });
  const notify = (m, p) => send({ jsonrpc: "2.0", method: m, params: p ?? {} });

  const dispose = () => {
    for (const { timer } of pending.values()) clearTimeout(timer);
    pending.clear();
    try { child.stdin.end(); } catch {}
    try { child.kill("SIGTERM"); } catch {}
  };

  return { request, notify, dispose, child };
}

export default function agoraExtension(pi) {
  let mcp = null;
  let started = false;

  pi.on("session_start", async () => {
    if (started) return;
    started = true;
    const command = process.env.AGORA_MCP_COMMAND || "agora-mcp";
    const env = {
      // Empty ON PURPOSE: forces agora-mcp onto its 0600 key cache, so no
      // bearer ever exists in this process tree.
      AGORA_API_KEY: "",
      AGORA_ADMIN_KEY: "",
      AGORA_URL: process.env.AGORA_URL || "",
      AGORA_AGENT_ID: process.env.AGORA_AGENT_ID || "",
      AGORA_HOME: process.env.AGORA_HOME || "",
      AGORA_ABOUT: process.env.AGORA_ABOUT || "",
    };
    mcp = mcpClient(command, env);
    try {
      await mcp.request("initialize", {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "agora-pi-bridge", version: "1" },
      });
      mcp.notify("notifications/initialized");
      const listed = await mcp.request("tools/list");
      for (const t of listed.tools || []) {
        const schema = t.inputSchema || { type: "object", properties: {} };
        pi.registerTool({
          name: "agora_" + t.name,
          label: "agora " + t.name,
          description: t.description || t.name,
          promptSnippet: "agora_" + t.name,
          parameters: {
            type: "object",
            properties: schema.properties || {},
            required: schema.required || [],
          },
          async execute(_id, params) {
            const res = await mcp.request("tools/call", {
              name: t.name, arguments: params || {},
            });
            const text = (res.content || [])
              .filter((c) => c.type === "text").map((c) => c.text).join("\n");
            if (res.isError) throw new Error(text || "agora tool failed");
            return { content: [{ type: "text", text }], details: {} };
          },
        });
      }
    } catch (e) {
      // LOUD, never silent: a seat whose bridge failed must say so in the
      // event stream instead of looking alive with no tools.
      console.error("agora bridge: startup failed: " + e.message);
    }
  });

  pi.on("session_shutdown", async () => {
    if (mcp) { mcp.dispose(); mcp = null; }
    started = false;
  });
}

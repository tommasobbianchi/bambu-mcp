"""Manual real-hardware run: slice a 10 mm cube and print it on the P1S through the MCP (ABS, tray 7)."""
import json, os, anyio, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

env = dict(os.environ)
env.pop("BAMBU_MCP_READONLY", None); env.pop("BAMBU_MCP_DRY_RUN", None)
P = StdioServerParameters(command=".venv/bin/python", args=["-m", "bambu_mcp"], cwd=".", env=env)
H = os.path.expanduser("~/solidcraft_work/p1s_cube")


def txt(res):
    return "\n".join(getattr(b, "text", str(b)) for b in res.content)


async def main():
    async with stdio_client(P) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("slice_and_print", {"printer": "p1s", "model_path": H + "/cube10.stl", "profile_dir": H,
                                                         "tray": 7, "expect_filament": "ABS", "expect_color": "898989FF",
                                                         "name": "cube10_abs_p1s", "watch": False})
            j = json.loads(txt(res)); print("submit", json.dumps(j)[:400], flush=True)
            jid = j["job_id"]
            t0 = time.time()
            while time.time() - t0 < 200:
                st = json.loads(txt(await s.call_tool("job_wait", {"job_id": jid, "timeout_s": 30})))
                print("job", json.dumps(st)[:500], flush=True)
                if st.get("state") in ("done", "failed", "cancelled", "interrupted"):
                    break
            ps = json.loads(txt(await s.call_tool("printer_status", {"printer": "p1s"})))
            print("p1s:", {k: ps.get(k) for k in ("state", "job", "progress_pct", "nozzle_c", "nozzle_target_c", "bed_c", "bed_target_c", "print_error", "stage")})
anyio.run(main)

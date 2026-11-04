# bambu_mcp

MCP server (Python, stdio) that lets Claude control Bambu Lab printers over the LAN
(MQTT/TLS on 8883, FTPS on 990) and offloads long work (slice -> upload -> print -> watch) to a
background job manager. Claude only polls (`job_status`) or long-polls (`job_wait`).

## Setup

```
cd ~/projects/bambu_mcp
uv venv .venv && uv pip install --python .venv/bin/python mcp paho-mqtt pytest
.venv/bin/python -m pytest -q tests          # mock-based, never touches a printer
.venv/bin/python tests/stdio_smoke.py        # read-only + dry-run against the real printers
```

Printer credentials live OUTSIDE the repo in `~/.config/bambu_mcp/printers.json` (mode 0600,
`{"a1mini": {"ip","serial","code","name"}, ...}`). If the file is missing it is fetched once from
`streamer-officina:/home/tommaso/mmu_say/bambu.json` over ssh (25 s hard timeout) and cached.
Access codes and serials are never logged or returned (they are scrubbed from all output; the FTPS
password is passed to curl through stdin, not argv).

## Tools

Read-only: `list_printers`, `printer_status`, `ams_info`, `recent_events`.

Jobs (return `job_id` immediately): `print_3mf`, `slice_and_print`, `slice_model`, `watch_print`;
manage with `job_status`, `job_wait(job_id, timeout_s<=240)`, `job_list`, `job_cancel`.

Control: `pause`, `resume`, `stop` (confirm), `set_light`, `set_speed`, `send_gcode` (confirm).

- `tray` is the AMS tray index `ams_id*4 + slot_index` (slot 3 of AMS 0 = `2`).
- `print_3mf` needs a sliced `.3mf` containing `Metadata/plate_N.gcode`. It refuses unless the printer
  is IDLE/FINISH and every tray matches `expect_filament` / `expect_color` (RRGGBBAA or RRGGBB).
- With `watch=true` the job follows the print to FINISH (done), FAILED (failed) or IDLE-after-run
  (done, "stopped"). `job_cancel` stops the server-side job only, not the physical print.
- `slice_and_print` uses OrcaSlicer CLI (`BAMBU_ORCA_BIN`, default
  `~/projects/orca-pr12998/build/src/orca-slicer`) with `machine_flat.json`, `process_flat.json`,
  `filament_flat.json` from `profile_dir`; outputs go to `~/.local/state/bambu_mcp/slices/<job_id>/`.

## Persistence

Job snapshots and print events (state changes, 10% progress milestones, errors, finish) are appended to
`~/.local/state/bambu_mcp/events.jsonl`. After a restart, old jobs are visible (`job_list`); in-flight
ones are marked `interrupted`, and a background re-attach starts a new `watch_print` job for any printer
that is still mid-print.

## Safety rules

- `stop` and `send_gcode` need `confirm=true`; set it only after explicit user approval.
- `dry_run=true` per call, or `BAMBU_MCP_DRY_RUN=1`: builds and returns the payload, never publishes or
  uploads. `print_3mf` dry-run still reads printer state (read-only) and reports `problems`.
- `BAMBU_MCP_READONLY=a1mini,p1s` (or `*`): every control command and print start for those printers is
  refused (dry-run still allowed). The smoke test forces `*`.
- Unit tests use a fake MQTT client and a fake curl; no network.

## Register in a Claude config (not done globally)

```json
{
  "mcpServers": {
    "bambu": {
      "command": "/home/tommaso/projects/bambu_mcp/.venv/bin/python",
      "args": ["-m", "bambu_mcp"],
      "cwd": "/home/tommaso/projects/bambu_mcp"
    }
  }
}
```

## Status of verification

Verified on hardware: read-only status of the A1 mini, FTPS/MQTT payload shapes are the ones proven
manually. NOT verified on hardware through this server: real print start/upload, pause, resume, stop,
light (`work_light` vs `chamber_light` node names), speed, gcode.

## License

MIT, see [LICENSE](LICENSE).

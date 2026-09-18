# Syncthing Setup — High-Frequency Sensor Ingestion

High-frequency sensor data means many small files arriving rapidly. The goal
of this configuration is: minimum latency, no partial/corrupt files ever
reaching `garage_uploader.py`, and low CPU overhead from rescans.

## 1. Topology

```
On-Prem Server  (writes sensor files)         PC / Gateway (uploads to Garage)
──────────────────────────────────            ─────────────────────────────────
Folder type:  Send Only                       Folder type:  Receive Only
                          ────── Syncthing ──────►
```

- **Send Only** on the on-prem server means Syncthing will never pull changes
  back onto the server — it is a pure source.
- **Receive Only** on the PC means Syncthing will never push local edits back
  to the server, and any local modification is treated as a "receive-only
  local change" you can safely revert — this keeps the folder a faithful
  mirror that `garage_uploader.py` can consume and delete from without
  fighting Syncthing.

## 2. Folder settings (both devices)

In the Syncthing Web GUI (`http://127.0.0.1:8384`) → your shared folder →
**Edit** → **Advanced**:

| Setting | Value | Why |
|---|---|---|
| Watch for Changes | **Enabled** | Uses filesystem notifications (inotify/ReadDirectoryChangesW) instead of periodic rescans — near-zero latency. |
| Full Scan Interval | `3600` (1 hour) | Watch for Changes handles real-time updates; the periodic full scan is just a safety net, so it can be infrequent to save CPU on directories with thousands of files. |
| Folder Type | `Send Only` (server) / `Receive Only` (PC) | See topology above. |
| File Pull Order | `Random` or `Oldest First` | Avoids head-of-line blocking if one large file is mid-transfer. |
| Ignore Patterns | `*.tmp`<br>`*.part`<br>`.stfolder` | Belt-and-suspenders on top of the atomic-write pattern below — Syncthing already ignores files that are still open for write on most platforms, but explicit ignores make behavior deterministic across OSes. |

## 3. Atomic writes on the sensor side

Syncthing (and `garage_uploader.py`) must never see a half-written file.
Always write to a temp name and rename into place:

```python
import json, os, tempfile

def write_sensor_file(records: list[dict], out_dir: str, name: str) -> None:
    tmp_path = os.path.join(out_dir, f".{name}.tmp")
    final_path = os.path.join(out_dir, f"{name}.json")
    with open(tmp_path, "w") as f:
        json.dump(records, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, final_path)   # atomic on POSIX and NTFS
```

`os.rename` is atomic within the same filesystem, so any watcher (Syncthing
or `watchdog` on the PC) only ever observes the fully-written `.json`/`.csv`
file appearing, never a partial one.

## 4. Verifying the pipeline end to end

```bash
# On-prem
echo '[{"ts": 1234, "value": 1.0}]' > /srv/sensor/out/.test123.tmp
mv /srv/sensor/out/.test123.tmp /srv/sensor/out/test123.json

# PC — should show up within a second or two if Watch for Changes is on
ls -la /path/to/syncthing/local/folder

# garage_uploader.py should log an "Uploaded test123.json" line
tail -f data/logs/garage_uploader.log
```

## 5. Common pitfalls

- **Two-way sync accidents**: don't leave a folder as `Send & Receive` on
  both ends — a delete or partial write on the PC side can propagate back
  and corrupt the source. Send Only / Receive Only is intentional.
- **Rescans thrashing CPU**: if `Full Scan Interval` is left at the default
  (60s) on a directory receiving hundreds of files/minute, Syncthing will
  spend most of its time hashing. Raise it once Watch for Changes is
  confirmed working.
- **Case-sensitivity / path length**: Windows on-prem servers writing very
  long sensor filenames can hit `MAX_PATH` issues — keep filenames short and
  prefer a `YYYYMMDD/HH/` subfolder structure over encoding the timestamp in
  the filename alone.

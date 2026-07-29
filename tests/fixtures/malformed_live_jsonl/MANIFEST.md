# Malformed `live.jsonl` fixtures

Each file simulates a different realistic way a live-timing capture file can go wrong.
All start with one or more well-formed lines (except the empty/single-truncated-line
cases) to simulate a session that was healthy before something went wrong.

| File | Failure mode |
|---|---|
| `truncated_final_line.jsonl` | Process killed mid-write: the final line is cut off partway through a base64 string, with no closing quote/brace — simulates a crash or kill during an in-flight `fwrite`. |
| `missing_type_field.jsonl` | A structurally valid JSON line is missing the required `Type` field (has `DateTime`/`Json` only) — simulates a schema drift or a hand-edited/replayed line. |
| `invalid_base64_z_payload.jsonl` | A `.z` topic (`CarData.z`) line has a `Json` value that is not valid base64 at all (`!!!not-valid-base64@@@###`) — simulates payload corruption before base64 decode. |
| `invalid_zlib_z_payload.jsonl` | A `.z` topic (`Position.z`) line has a `Json` value that decodes as valid base64 but the resulting bytes are not a valid zlib stream — simulates corruption introduced after encoding but before decompression. |
| `z_payload_decompresses_to_non_json.jsonl` | A `.z` topic (`CarData.z`) line's payload is valid base64 and valid zlib, but the decompressed bytes are plain text, not JSON — simulates a topic mismatch or upstream bug that compressed the wrong content. |
| `unparseable_datetime.jsonl` | Three otherwise well-formed `WeatherData` lines have unparseable `DateTime` values: an empty string, free-text garbage (`"not-a-timestamp"`), and a wrong-format timestamp (`"18/07/2026 14:03:22"`) — simulates a clock/formatting bug upstream. |
| `embedded_null_byte.jsonl` | An otherwise well-formed `RaceControlMessages` line has a stray `\x00` null byte spliced into the middle of a string value — simulates buffer corruption from a flaky write or disk issue. |
| `non_utf8_bytes.jsonl` | An otherwise well-formed `DriverList` line has raw invalid UTF-8 bytes (`\xff\xfe`) spliced into a driver name field — simulates network-layer encoding corruption (e.g. a dropped/garbled byte in a non-UTF8-safe transport hop). |
| `empty_file.jsonl` | Completely empty file (0 bytes) — simulates a capture process that was started/created but killed or crashed before a single message was ever written. |
| `single_truncated_line.jsonl` | File contains exactly one line, and that line is truncated mid-token (`"Meeting": "Silverst` with no closing) — simulates an immediate crash on the very first write, with no valid content at all. |
| `corrupted_line_in_middle.jsonl` | Seven valid lines surround one corrupted line in the middle (a `CarData.z` line truncated mid base64, no trailing `"}}`) — simulates a single transient write glitch (e.g. a momentary disk-full or partial flush) in an otherwise healthy, long-running capture, rather than total file corruption. |
| `mixed_line_endings.jsonl` | Lines are written with inconsistent line terminators — some `\r\n`, some bare `\n` — within the same file, each individual line's JSON is valid — simulates a capture written across a platform boundary or through a tool that doesn't normalize line endings consistently. |

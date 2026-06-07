"""
F1 Live Timing - Backend Processing Pipeline.

Server-side processing of F1 timing data. Replaces browser-side computation
with a pipeline of:

    File Reader → Message Bus → Processors → Session Engine → WebSocket

The JSONL file is the decoupling point between capture (SignalR) and playback.
The File Reader doesn't know or care whether the file is complete (replay) or
being actively written to (live).
"""

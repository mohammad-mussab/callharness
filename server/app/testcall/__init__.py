"""Automated test calls: dial the agent's real number and talk to it.

The smoke test for "I shipped a change I cannot see locally — is it working in
production?". Not an eval suite: a handful of calls a week, placed by hand, against
the live number. Evals belong in text against a staging agent, where they cost
nothing; this exists precisely because the thing being tested is the real phone
path, IVR and all.

Four pieces, in the order a call moves through them:

    runner.py    owns the run: dials, waits, matches, judges, cleans up
    twilio.py    places the call and presses the keypad digits
    bridge.py    the websocket Twilio streams call audio to
    realtime.py  the OpenAI session that listens and speaks as the caller

The audio never touches this process's CPU in any meaningful way: Twilio and the
Realtime API both speak G.711 µ-law at 8kHz, so the bridge relays base64 frames
between two sockets without decoding them.
"""

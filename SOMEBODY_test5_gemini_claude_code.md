# Task for Claude Code: SOMEBODY Test 5 (Gemini camera predictions)

## Context
- Tests 1 to 3 pass: the Mega sends tilt events, `server.py` relays them, and `public/index.html` on my iPhone (over a cloudflared HTTPS tunnel) shows the phrase grid. RIGHT/LEFT move the highlight and NOD selects.
- Test 4 (ElevenLabs) may or may not be finished. If `/tts` exists, use it. If not, use `speechSynthesis` and leave a clear `TODO` where `/tts` plugs in.
- The phone's back camera already shows a live preview but isn't used yet.

## Goal
The phone camera looks at what's in front of the user. Gemini suggests 4 short phrases that fit the scene (a cup → "I'm thirsty"), and they appear as the **first tiles in the scan order**, so the user reaches them in 1 or 2 tilts instead of scrolling.

**Core rule: the camera is a speed boost, never a dependency.** If the camera, the network or Gemini fails, the fixed grid keeps working exactly as before.

## Ground rules
- Ask me before installing anything.
- The **Gemini API key never goes into the HTML or into git.** Read it from `.env` as `GEMINI_API_KEY`. Only `server.py` calls Gemini, never the phone.
- Don't break existing paths: arrow keys, `/fake/...`, cached audio.
- If an API call fails, show me the exact error and check the current Gemini API docs. Don't guess model names or fields.

---

## Step 1: Key and model check
1. Ask me for the key and write it to `.env`. Never print it back.
2. List the available models (`GET https://generativelanguage.googleapis.com/v1beta/models?key=...`). Pick the **fastest current "Flash" model that accepts images**; try `gemini-2.5-flash` first if it's listed. Save the choice to `.env` as `GEMINI_MODEL` and tell me which one you picked.
3. Send one test request with a tiny image (a solid-color JPEG) and report the latency.

## Step 2: Server endpoint `POST /predict` in `server.py`
Request body (JSON):
```json
{ "image_b64": "<jpeg base64, no data: prefix>",
  "recent": ["I need water", "Thank you"],
  "local_time": "14:05" }
```
What the server does:
- Calls `models/{GEMINI_MODEL}:generateContent` with the image as `inline_data` (`mime_type: image/jpeg`) plus the prompt below.
- Asks for JSON output (`generationConfig.responseMimeType: "application/json"`, with a `responseSchema` if the model supports it). If the model supports a thinking budget, turn thinking off or to the minimum, for speed.
- **Timeout: 4 s.** On a timeout, error or 429, return `{"ok": false, "reason": "..."}` quickly. Never hang.
- Validates the output: exactly 4 phrases, each 1 to 6 words, deduplicated, with no phrase already on the fixed grid. Trim or drop anything invalid.
- Logs: latency in ms, the `scene` text and the phrases. **Never saves images to disk.**

Response:
```json
{ "ok": true, "scene": "a glass of water on a table", "phrases": ["I'm thirsty", "Water please", "Can you pour it", "Thank you"], "confidence": 0.82, "ms": 1430 }
```

### Prompt (use as-is, tune later)
> You help a person who cannot speak or use their hands. They communicate by choosing phrases. Look at the photo from the camera in front of them and suggest the 4 phrases they are most likely to want to say right now.
> Rules: first person, natural spoken English, 1-6 words each, practical needs first (drink, food, comfort, help, people). If a person is visible, include a social phrase (e.g. "Hi", "Thank you", "Can you help me?"). Never identify or name people, never guess age/gender/ethnicity, never give medical advice. If the image is dark, blurry or empty, return low confidence and generic helpful phrases.
> Context: recent phrases said = {recent}; local time = {local_time}.
> Return JSON: {"scene": "<6-10 word description>", "phrases": [4 strings], "confidence": 0.0-1.0}.

## Step 3: Phone side (`public/index.html`)

**Capturing frames**
- After Start, grab a frame from the existing camera `<video>` into a canvas **downscaled to 512 px on the long side**, JPEG quality 0.6.
- **Only send a frame when the scene changed**: keep a 16×16 grayscale thumbnail and send if the average pixel difference from the last sent frame is above a threshold (start at about 12/255). Also send **at most every 4 s**, and **at least every 20 s**.
- Only one request in flight at a time.
- On a 429 or error, back off: 4 s → 8 s → 16 s (cap 30 s). Reset after a success.
- A toggle **"Camera suggestions: on/off"** plus a small dot that shows when frames are being sent (privacy).

**Showing suggestions**
- Add a **"Suggested" row of 4 tiles above the fixed grid**. In the scan order, the suggestions come first, so one RIGHT from the start lands on suggestion 1.
- Show a small caption "Seeing: a glass of water on a table" and the latency.
- **No tiles jumping under the user:** only swap in new suggestions if there has been **no tilt event in the last 3 s**. Otherwise queue them and apply once the user is idle. This matters a lot.
- If `confidence < 0.4`, keep the previous suggestions.
- When the camera is off or Gemini fails, the Suggested row comes from **local fallbacks**: the time of day (morning → "Good morning", 11:30–13:30 or 17:30–19:30 → "I'm hungry", after 21:00 → "I'm tired") plus the 2 most recent phrases. Label the row "Suggested (offline)".

**Speaking suggestions**
- As soon as new suggestions arrive, **prefetch their audio** with `/tts?text=...` (if it exists) into blobs, so a NOD plays instantly.
- On a NOD on a suggestion: play the prefetched clip, else live `/tts`, else `speechSynthesis`.

## Step 4: Measurement logging (this is the slide number)
- On every selection, log to the server: `POST /log` with `{phrase, source: "suggested"|"fixed", moves_since_last_select, ms_since_first_move}`. Append to `trials.csv`.
- Add `GET /stats`, which returns the averages of moves and time per selection for `suggested` vs `fixed`.

---

## Step 5: Test with me. Report PASS/FAIL for each.

**A. Scene tests.** I hold the phone (or put it on a stand) facing each item for about 5 s. For each scene, you record the scene text, the 4 phrases and the latency. Ask me to rate each scene **sensible / not sensible**.
1. A cup or water bottle
2. A plate of food or a snack
3. A person facing the camera
4. A phone
5. A pillow or blanket
6. A closed door
7. A window
8. A TV remote or a laptop
9. A book
10. A pill bottle (expect something like "I need my medicine", with no medical advice)

**Pass:** at least 8 of 10 rated sensible, **median latency under 3 s**, and no response that names or describes a person's identity.

**B. Bad input.** Phone camera covered with a hand (dark), and a deliberately blurry shot. **Pass:** low confidence, the previous suggestions are kept, nothing crashes.

**C. Stability.** I keep tilting continuously for 15 s while you change the scene. **Pass:** the Suggested row never changes mid-tilt, and it updates within about 3 s after I stop.

**D. Failure modes.**
1. Set a wrong API key and restart. The row switches to "Suggested (offline)" and the grid works normally.
2. Turn on airplane mode on the phone after Start. The same thing happens, and cached phrases still play.
3. Turn the camera toggle off. No frames are sent (the dot is off), and the offline suggestions show.

**E. Speed proof for the slide.** With a cup in view, the task is "say I'm thirsty or I need water":
- 5 trials with suggestions **on** and 5 with suggestions **off** (fixed grid only). Reset the highlight to the start before each trial.
- Report the average moves and average seconds for each, from `/stats`. **Pass:** suggestions reduce moves and time.

## Final report
- The model used, the tables for A to E, the median latency and the numbers from E.
- The 3 worst predictions, with one prompt tweak each that would fix them. Don't apply the tweaks without asking me.
- Rough cost: requests per minute while idle vs while moving.

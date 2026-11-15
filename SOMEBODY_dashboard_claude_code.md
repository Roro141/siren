# Task for Claude Code: Caregiver dashboard + "Ask a question"

## Context
These already work: `server.py` (tilt events from the Mega, WebSocket to the phone, `/tts` ElevenLabs with caching, `/predict` Gemini camera suggestions, `/log`, `/stats`), `public/index.html` on the iPhone (phrase grid, Suggested row, cloned voice), and `profile.json` (condition, devices used, people, likes).

Goal: a laptop page at `/dashboard` for a caregiver, with 3 panels plus the headline feature, **Ask a question**. The caregiver types a question, Gemini generates short answer options, the options appear on the phone, the user picks one with head tilts, and the answer is spoken in their voice and shown on the dashboard.

## Ground rules
- Ask before installing anything. Keys stay in `.env`, server-side only.
- Don't break any existing phone behavior, fallbacks or test paths.
- Plain HTML/JS, no build step. Desktop layout, clean and calm (match the phone's dark theme).
- If any Gemini/ElevenLabs call fails, show the exact error, check the current docs, don't guess.

---

## Feature 1: Ask a question (build this first)

### Dashboard side
- A text box: "Ask {name} a question…" and a **Send** button (Enter also sends).
- Optional, a stretch goal: a mic button that uses the browser's Web Speech API (Chrome) to fill the text box. If it's unsupported, hide it.

### Server: `POST /ask` with `{ "question": "..." }`
1. Call Gemini (`GEMINI_MODEL`, JSON output, thinking off/minimal, **4 s timeout**) with this prompt:

> You are helping a person with ALS answer a question from their caregiver. They cannot speak or use their hands; they choose from short options using head movements, so fewer, clearer options are better.
> Question: "{question}"
> About them: {profile.json}
> What the camera currently sees (may be empty): "{last_scene}"
> Recent things they said: {recent}
> Return JSON: {"kind": "yes_no" | "choice" | "open", "options": [3 strings]}
> Rules for options: first person, natural spoken English, 1–6 words, distinct from each other, kind and dignified, never medical advice, never invent facts about people.
> - yes_no questions: options must be exactly ["Yes", "No", "Not right now"] unless a more specific third option is clearly better (e.g. "Yes, but later").
> - choice questions (e.g. "Tea or coffee?"): the choices offered, plus one sensible alternative.
> - open questions (e.g. "What do you want for dinner?"): 3 specific, likely answers based on the profile and context.

2. Validate: exactly 3 options, each 1–6 words, deduplicated. **The server always appends a 4th option, "Something else"**, which tells the caregiver to ask differently or lets the user go back to the full grid.
3. **Fallback** if Gemini fails or times out: the options are ["Yes", "No", "Not right now", "Something else"], and the dashboard says "Using basic answers (AI unavailable)".
4. Prefetch audio for all 4 options with `/tts` (in parallel), so the user's answer plays instantly.
5. Broadcast to the phone over the WebSocket: `{"type": "QUESTION", "id": ..., "question": ..., "options": [...]}`.

### Phone side (`index.html`)
- On QUESTION: show a clear banner at the top, "Sam asks: Do you want the TV on?" (use the asker name from `profile.json`, or "Someone asks"). Replace the Suggested row with the 4 answer options, styled as answers (distinct color), and move the highlight to the first option.
- Navigation works as usual. NOD on an option speaks it (cached audio → live `/tts` → `speechSynthesis`), sends `{"type": "ANSWER", "id": ..., "answer": ...}` back to the server, and returns the row to normal suggestions.
- "Something else" speaks "Something else" and returns to the normal grid.
- If the question isn't answered in 3 minutes, clear it quietly.
- **No tile jumping:** if the user tilted in the last 1 s, wait until they pause before swapping the row in.

### Dashboard shows
- The question and the options it generated, the time Gemini took, then the **answer, highlighted**, with how long the user took to answer.

---

## Feature 2: Live conversation + alerts
- A chat-style log, newest at the bottom. Each entry: the time, what was said, and its source (grid / suggested / answer to a question). Questions and answers are shown as a pair.
- **Urgent phrases** (configurable list in `profile.json`, default: "I need help", "I'm in pain", "Suction please", "I can't breathe"): a full-width red banner on the dashboard plus an alert sound, until the caregiver clicks "On my way". Clicking it plays "Sam is coming" on the phone.
- Hook: call `notify_watch(text)` on urgent phrases. For now it only logs. A later step will add the Apple Watch.

## Feature 3: Under the hood (for judges)
- A live line chart of the roll and pitch angles, if the Mega forwards them. If not, just show the event markers. Keep it lightweight (canvas, no library, or one small CDN library).
- An event list with latency.
- Camera: the last `scene` caption, the 4 suggestions and the Gemini latency.
- Health dots: Sensor (serial connected), Phone (WebSocket clients ≥ 1), Gemini (last call ok), ElevenLabs (last call ok), Internet. Green / amber / red, with the last error on hover.

## Feature 4: Add a phrase
- A text box plus a category picker. It saves to `phrases.json`, generates the audio with ElevenLabs, pushes an update to the phone so the new tile appears **at the end of the grid** (it never reorders existing tiles), and shows "Ready in her voice" when the audio is cached.

---

## Test with me (PASS/FAIL each)
1. **Yes/no:** "Do you want the TV on?" → Yes / No / Not right now / Something else. I answer by tilting, and it's spoken and logged.
2. **Choice:** "Tea or coffee?" → includes Tea and Coffee.
3. **Open:** "What do you want to watch tonight?" → uses `profile.json` likes (e.g. a Mavericks game).
4. **Context:** with a blanket in camera view, "How are you feeling?" → options reflect comfort/temperature.
5. **Speed:** from Send until the options appear on the phone, under about 3 s. Report the median of 5.
6. **AI down:** wrong Gemini key → basic answers appear, and nothing breaks.
7. **Urgent:** I select "I'm in pain" → red banner and sound. "On my way" → the phone speaks the reply.
8. **Add phrase:** a new phrase appears at the end of the grid and plays in the cloned voice.
9. **Health dots:** unplug the Mega → the Sensor dot goes red, then recovers when replugged.

## Final report
A PASS/FAIL table, the median `/ask` latency, the 3 best and 3 worst generated option sets, and the URL to open the dashboard.

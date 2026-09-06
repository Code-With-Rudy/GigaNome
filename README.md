# Sync Metronome

A metronome that keeps a whole band's phones/laptops clicking on the same
beat, over the same Wi-Fi network. One person hosts (sets BPM + time
signature, hits Start); everyone else joins with a 4-character code and
follows automatically.

## How the sync actually works

Two things had to be solved for this to feel tight enough for live use:

1. **Whose clock do we trust?** Every phone's clock is slightly off from
   every other phone's. The server measures round-trip latency for each
   client (a handful of ping/pong messages) and each client computes its
   own offset from server time, using the sample with the lowest latency
   (the classic "cheapest path wins" trick, similar to how NTP works).

2. **How do we get a click to fire at the *exact* right instant?**
   Browser timers (`setTimeout`/`setInterval`) are not precise enough —
   they can drift by tens of milliseconds. Instead, once a client knows
   "beat 1 happens at server-time T," it converts T into a time on its
   own **Web Audio clock** (which is sample-accurate) and schedules the
   click sound ahead of time. The server never streams individual beats
   over the network — it only ever sends "tempo X, time signature Y,
   start at time T," and each device does its own precise scheduling
   from there. This is what keeps everyone tight even with normal Wi-Fi
   jitter.

Realistically, on a home/venue Wi-Fi network you should expect sync
within roughly 10-40ms between devices — plenty tight for a live band,
though not laboratory-perfect. If two phones are visibly out of step,
it's almost always Wi-Fi latency, not a bug — try a less congested
network or a dedicated hotspot for the gig.

## Running it

```bash
pip install -r requirements.txt
python app.py
```

This starts a server on port 5000. Find the host machine's LAN IP:

- **Mac/Linux:** `ifconfig | grep "inet "` (look for something like `192.168.1.42`)
- **Windows:** `ipconfig` (look for "IPv4 Address")

Then, with every device on the **same Wi-Fi network**, open:

```
http://<that-ip>:5000
```

One person taps **Start a session** (becomes the host, gets a 4-character
code). Everyone else taps **Join a session** and types that code in.

## Deploying it publicly (GitHub + Render or Railway)

This app keeps session state (who's in which room, current tempo) in memory
in a single Python process, so it needs to run as **one always-on process**,
not stateless serverless functions — that's exactly what Render/Railway give
you, and it's why the code already ships with a `Procfile` and (for Render)
a `render.yaml`.

**1. Push it to GitHub**

```bash
cd metronome_sync
git init
git add .
git commit -m "Sync metronome"
gh repo create sync-metronome --public --source=. --push
# no gh CLI? create an empty repo on github.com instead, then:
# git remote add origin https://github.com/<you>/sync-metronome.git
# git branch -M main && git push -u origin main
```

**2. Deploy — Render (has a permanent free tier for this kind of app)**

- Go to render.com → **New +** → **Blueprint** → connect the GitHub repo.
  Render reads `render.yaml` automatically and sets everything up.
- No Blueprint option, or prefer doing it by hand? **New +** → **Web
  Service** → connect the repo → Render auto-detects Python. Set:
  - Build command: `pip install -r requirements.txt`
  - Start command: `gunicorn --worker-class gthread --threads 8 -w 1 --timeout 120 app:app`
- Free tier note: the service sleeps after inactivity and takes ~30-60s to
  wake on the next visit — fine for testing, worth a paid instance ($7/mo)
  if you need it awake and instant for a real gig.

**2. Deploy — Railway (alternative)**

- railway.app → **New Project** → **Deploy from GitHub repo**.
- Railway auto-detects the `Procfile` and uses it as the start command; no
  extra config needed. Add a public domain under **Settings → Networking**.

**Important: keep it to a single instance/worker.** The `-w 1` in the start
command is deliberate — session data lives in that one process's memory.
If you ever scale to multiple instances or workers, different band members
could land on different processes with different session state, and joins
would randomly fail. For a band-sized tool this is a non-issue; just don't
bump the instance/worker count.

Once deployed, everyone can join over the internet instead of needing the
same Wi-Fi network — same code, same join-code flow, just a public URL
instead of a LAN IP.

## Notes / current limitations (this is a testing build)

- Running locally (`python app.py`), everyone needs the same Wi-Fi network.
  Deployed to Render/Railway (see below), it works over the internet
  instead — same flow, just with slightly higher (though still
  corrected-for) latency than a local network.
- Only the host can change tempo/time signature/start/stop — deliberate,
  so the whole band always follows one source of truth.
- If the host closes their tab, the session ends for everyone (no
  automatic hand-off yet).
- Time signature currently controls the accent pattern (first beat of
  the bar is a distinct, louder/higher click) rather than subdividing
  by note value.

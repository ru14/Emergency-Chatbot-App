# Demo script (about 3 minutes)

Synthetic data only. Say once at the start: "This is decision support for a human dispatcher. It is a demo, not
clinically validated, and it never dispatches or gives treatment advice on its own."

**Before you start**
- Be on the network whose IP the firewall allows (`4.53.158.54`), or add the current IP to `sea-copilot-fw` first.
- Open `http://172.232.188.234` and log in as `dispatcher` with `APP_PASSWORD` from `.env`.
- Open a second tab at `http://172.232.188.234/health`.
- Have a terminal ready in the repo folder for the restart in step 6.
- Optional: open Cloud Manager on the Linode, firewall and database pages.

| # | Do | Say | Judging |
|---|---|---|---|
| 1 | Show the `/health` tab: `host: sea-copilot-web`, `inference: configured (qwen3-8-27b)`, `database: connected (N incidents stored)`. Optionally flip to Cloud Manager for the Linode, firewall and database | "The app runs on Akamai Cloud Compute in Seattle behind a Cloud Firewall, and stores every incident in Akamai Managed PostgreSQL." | 40% services |
| 2 | Click **Medical: collapsed man at a bus stop**, then **Analyze call**. Point at the alerts first, then wait for the facts | "Dispatchers lose seconds hunting for life threats. These alerts came from a keyword screen in milliseconds, with no AI. Now Qwen 3 on Akamai Inference extracts facts, and every fact must quote the caller's exact words." Point at the model ID, latency and request ID line | Usefulness, 20% inference |
| 3 | Tick **Plant a false fact**, click **Analyze call** again | "We don't trust the model. Here I've planted a fact the caller never said: heavy bleeding. Validation rejects it because the quote isn't in the transcript. A flipped polarity, like 'not breathing' becoming 'breathing', is rejected the same way." | Creativity, inference checks |
| 4 | Untick it. Tick **Simulate AI outage**, click **Analyze call** | "If the AI is down or slow, the dispatcher still gets the alerts, the protocol card and the questions to ask. The safety net doesn't depend on the model." | Creativity |
| 5 | Untick it. Click **Fire: kitchen fire in an apartment**, then **Analyze call** | "Fire call: someone is trapped. The protocol card is fixed text chosen by rules, never written by the model. Missing facts tell the dispatcher what to ask next: here, hazards like gas tanks." | Usefulness |
| 6 | Click **Confirm** on "people trapped". Add a sentence to the transcript and click **Analyze call** (it becomes revision 2). In the terminal run `ssh root@172.232.188.234 systemctl restart copilot`, then reload the page | "Every decision is saved in PostgreSQL with the transcript revision it was made on. After a server restart, the incident and the review are still here. If the transcript changes, a review of the old version is refused." | 40% services |
| 7 | Close | "The AI is treated as an untrusted witness: it must cite evidence, its mistakes are shown, and a non-AI safety net keeps working. Built on Akamai Compute, Cloud Firewall, Managed PostgreSQL and Qwen 3 on Akamai Inference." | All |

**If something goes wrong**
- The page or SSH times out: your IP changed. Add it to the firewall rule and click **Save Changes**.
- An AI error shows: that is step 4's point. Carry on and mention it, then retry.
- Qwen is slow (about 15 s): keep talking over the alerts, which are already on screen.

**Proof of the 409 without the page:** see the live run in [service-inventory.md](service-inventory.md#managed-postgresql).

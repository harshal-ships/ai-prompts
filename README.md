Hello all! Manual clinic reservation channels create high operational friction, prolonged hold times, and scheduling inefficiencies. Traditional touch-tone phone menus fail to process natural speech, leading to frequent call abandonment, data entry errors, and poor schedule capacity utilization.

This tutorial resolves these friction points by deploying an autonomous clinic receptionist voice system named Claudia. The architecture uses the Telcoflow SDK to stream full-duplex telephony audio and hooks it directly into Gemini Live to run snappy, real-time voice conversations. We are explicitly avoiding complex, heavy middleware frameworks like the Google Agent Development Kit, keeping the audio layer perfectly lightweight. Once the call ends, automated post-call workflows process the transcript through OpenClaw to extract details, write to the calendar via the gog CLI, and push confirmation summaries directly to WhatsApp, removing administrative overhead entirely.

Every developer diving into Voice AI eventually runs into the exact same realization: voice processing is probabilistic, but backend database writes must remain entirely deterministic.

If you build a monolithic voice agent where your live voice model attempts to query calendar APIs, parse open slots, and trigger messaging webhooks mid-conversation, things will fall apart. A tiny spike in network latency or a temporary endpoint timeout causes awkward silences inside the phone line. Even worse, if the voice engine hallucinates an open availability window before real confirmation takes place, you end up promising a busy slot to a customer.

The solution is full architectural decoupling. Let your voice interface focus purely on a natural, snappy conversation. The moment the user hangs up, hand off the recorded transcript to an automated background workflow that handles validation, schedules the appointment, and dispatches text confirmations calmly in a separate execution phase.

## 1\. The Decoupled Integration Layout

To shield the active speech channel from downstream fulfillment processing drops, data collection is handled independently from final ledger orchestration:

![](https://cdn.hashnode.com/uploads/covers/650a840758b09d1fee8d8588/7424d683-99d2-4ab0-ad4c-56fbc94bc3d8.jpg align="center")

## 2\. Step-by-Step Integration

### Step 2.1: Prepare Your Server Environment

Log in to your Ubuntu cloud instance via SSH, refresh your package managers, and confirm Python 3.12 along with core development dependencies are installed:

```shell
sudo apt update && sudo apt upgrade -y
sudo apt install software-properties-common -y
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install python3.12 python3.12-venv python3.12-dev -y
```

### Step 2.2: Deploy Node.js and Global OpenClaw

OpenClaw coordinates our automated post-call transcription extraction and manages local WhatsApp messaging loops. Install Node.js followed by the openclaw package utility globally on your machine:

```shell
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g openclaw
```

Make sure the `gog binary` is available so OpenClaw skills can reference calendar operations down the line.

### Step 2.3: Clone the Project Repository and Install Dependencies

Instead of building folders manually, pull down the integration repository directly onto your system:

```shell
git clone https://github.com/harshal-ships/Appoint-booking-agent-with-OpenClaw.git
cd Appoint-booking-agent-with-OpenClaw/"appointment booking agent"
```

Initialize an isolated virtual workspace utilizing Python 3.12, activate it, and update your project library requirements:

```shell
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 2.4: Authenticate gog on the Server (Headless Remote OAuth Flow)

Without a functioning Google Calendar bridge, your agent will completely fail to process write instructions or track appointments. The gog CLI utility provides a remote OAuth integration mechanism designed specifically for headless SSH servers.

First, open your Google Cloud Console, generate an OAuth credential key configuration pinned to the Desktop app client type, download the credential file, and upload it to your instance directory as `~/client_secret.json`.

On your server terminal, initialize the application tracking key:

```shell
gog auth credentials set ~/client_secret.json --client healthfirst
```

Now, initialize Step 1 of the headless remote authorization loop to print out your target sign-in URL string:

Bash

```shell
gog auth add clinic@gmail.com --client healthfirst --services calendar --remote --step 1
```

Copy that full printed URL address, open it on your phone or any external web browser, sign in using your clinic's Google account, and grant access tokens. The web page will eventually redirect to a local or broken address (such as [`http://127.0.0.1`](http://127.0.0.1)`...` or [`http://localhost`](http://localhost)`...`): this is completely normal. Copy that complete redirected URL out of your browser's address bar.

Return to your server terminal and initialize Step 2, wrapping the captured redirect string inside single quotes to complete verification:

```shell
gog auth add clinic@gmail.com --client healthfirst --services calendar --remote --step 2 --auth-url 'PASTE_THE_FULL_REDIRECT_URL_HERE'
```

Verify your configuration tokens are communicating correctly with Google by printing your active calendars list:

```shell
gog calendar calendars --json
```

### Step 2.5: Link Your WhatsApp Account via OpenClaw

To link your messaging backend directly with a phone number, run OpenClaw's interactive terminal login sequence:

```plaintext
openclaw channels login --channel whatsapp
```

The terminal will instantly generate a text QR code directly within your command line interface. Open WhatsApp on your phone, go to Linked Devices, choose Link a Device and scan the terminal screen to hook up your communication links safely.

### Step 2.6: Populate Your Environment Control File

Generate your configuration mapping file (nano .env) to shield necessary authorization credentials:

```shell
WSS_API_KEY=your_agentao_voice_token WSS_CONNECTOR_UUID=your_agentao_connector_id GOOGLE_API_KEY=your_google_ai_studio_key GOOGLE_CALENDAR_ID=primary 
GOG_ACCOUNT=clinic@gmail.com
```

### Step 2.7: Direct Terminal Process Execution

To run the entire system layout, you do not need to deal with complex Linux service wrappers. Simply open two separate terminal tabs to launch your active framework scripts directly:

In your **first tab**, activate your long-running messaging gateway server to process outbound text payloads:

```shell
openclaw gateway
```

In your **second tab**, source your python environment and activate your inbound voice agent listener directly:

```shell
source .venv/bin/activate
python booking_agent.py
```

![](https://cdn.hashnode.com/uploads/covers/650a840758b09d1fee8d8588/105cd8c6-0a22-4fd4-9b8a-f5c7a09a9584.png align="center")

## 3\. Reviewing Post-Call Output Artifacts

The moment a patient concludes their call session, the asynchronous controller processes the transcript automatically, executing calendar updates and triggering targeted alerts.

### Verified Google Calendar Bookings

When a scheduling request passes validation checks, the deterministic backend issues a command via the gog tool to write a secure entry directly into the calendar. This safely registers the appointment details and links the unique call tracking identifier.

![](https://cdn.hashnode.com/uploads/covers/650a840758b09d1fee8d8588/d120532f-8161-48b9-84d9-9883d0fadde1.png align="center")

### Dual-Audience WhatsApp Notifications

Text delivery routes messaging payload output precisely according to the client's explicit opt-in statements gathered during the live interaction:

*   **The Patient Booking Receipt (Opt-In Dependent):** If the user agreed to receive updates on WhatsApp, the platform parses their number into an E.164 string format and routes a concise confirmation receipt summarizing their appointment type, date, and clinic arrival time.
    
*   **The Clinic Operations Summary (Mandatory Alert):** a diagnostic summary is sent straight to the clinic's office device. This diagnostic message outputs validation status, customer details, and call metrics so the team always stays in the loop.
    

![](https://cdn.hashnode.com/uploads/covers/650a840758b09d1fee8d8588/2c6e70c0-11d9-4e67-8ed7-1b800dce6062.jpg align="center")

## 4\. Wrapping Up and Experimenting

One of the best things about this decoupled architecture is how customizable it is. Because the real-time dialogue boundaries are isolated within standard environment strings inside `booking_agent.py`, you can jump straight into `CLAUDIA_SYSTEM_PROMPT` to tweak the phrasing, inject customized clinic policies, adjust voice characteristics, or add regional localization variables. You can refactor the voice behavior endlessly without breaking a single line of your downstream text processing or calendar storage blocks.

Give this configuration a spin on your server instances, try varying the system prompts, and see how the background automation behaves. If you hit any friction points or discover ways to sharpen the scheduling logic, drop a comment below or open an issue on the repository. Any community feedback or implementation reviews are highly appreciated as we build out better conversational infrastructure patterns!

### References

*   [AgenTao SDK Documentation](https://docs.agentao.com/)
    
*   [OpenClaw Documentation](https://docs.openclaw.ai/)
    
*   [Gemini Documentation](https://ai.google.dev/gemini-api/docs/live-api)
    
*   You can find the complete implementation, environment guides, and repository configuration files here: [https://github.com/harshal-ships/Appoint-booking-agent-with-OpenClaw](https://github.com/harshal-ships/Appoint-booking-agent-with-OpenClaw)

# NoUI

> **Skip the UI. Turn any website into fast, reliable APIs for
> agents.**\
> *Go beyond Claw. Call the underlying APIs.*\
> *Skip Computer-use Agents.*

------------------------------------------------------------------------

## 🚀 What is NoUI?

**NoUI turns any website into an API your agents can call.**

Instead of automating clicks and scraping the UI, NoUI:
1. Records how you use a website
2. Extracts the underlying APIs
3. Converts them into callable Python functions
4. Exposes them via MCP for agents

No clicks. No DOM parsing. No brittle automation.

------------------------------------------------------------------------

## ⚡ Why NoUI?

Computer-use agents simulate humans: - 🐢 Slow (UI loops, page loads) -
💸 Expensive (token-heavy, step-heavy) - 🧱 Fragile (break on UI
changes)

**NoUI executes software directly:**
- ⚡ **Fast** --- direct API calls
- 💸 **Cheap** --- fewer steps, fewer tokens
- 🎯 **Reliable** --- uses the same APIs the app uses internally

> **Stop automating clicks. Execute software.**

------------------------------------------------------------------------

## 🧠 How it works

1.  Record a session (Chrome extension + voice)
2.  Extract HAR traces + intent
3.  Convert into Python API functions
4.  Maintain authenticated sessions via Tabby
5.  Expose everything as an MCP endpoint
6.  Agents call APIs instead of clicking UI

------------------------------------------------------------------------

## 🏗️ Architecture

\[ User + Chrome Extension \]\
↓ (HAR + Voice)\
\[ NoUI Compiler \]\
↓\
\[ Python API Functions \]\
↓\
\[ Tabby Runtime \]\
(persistent browser sessions + auth)\
↓\
\[ FastAPI Service \]\
↓\
\[ MCP \]\
↓\
\[ Claude / ChatGPT / Agents \]

------------------------------------------------------------------------

## 🔑 Core Components

### 🧩 HAR → API Compiler

-   Parses browser network traffic (HAR)
-   Groups requests into logical workflows
-   Generates clean Python functions

### 🐾 Tabby Runtime

-   Keeps browser sessions alive in the cloud
-   Handles cookies, headers, auth
-   Streams VNC for login / 2FA when needed

### 🔌 MCP Server

-   Exposes generated APIs as tools
-   Works with Claude, ChatGPT, and MCP-compatible agents

------------------------------------------------------------------------

## 🎯 What you can do

-   Automate websites with no public APIs
-   Turn internal tools into agent-ready SDKs
-   Replace brittle browser automation workflows
-   Build production-grade agents that actually scale

------------------------------------------------------------------------

## ⚡ Example

``` python
def create_invoice(customer_id, amount):
    return call_api(
        method="POST",
        endpoint="/api/invoices",
        headers=session_headers,
        json={
            "customer_id": customer_id,
            "amount": amount
        }
    )
```

Then your agent simply says:

> "Create an invoice for customer X"

NoUI executes it directly.

------------------------------------------------------------------------

## 🔐 Authentication Flow

1.  NoUI spins up a remote browser session
2.  Streams it via VNC
3.  You complete login / 2FA once
4.  Tabby maintains the session

Agents reuse the authenticated context automatically.

------------------------------------------------------------------------

## ⚔️ NoUI vs Computer-Use Agents

|               | Computer-Use Agents  | NoUI               |
|---------------|----------------------|--------------------|
| Speed         | Slow (UI loops)      | Fast (direct APIs) |
| Cost          | High                 | Low                |
| Reliability   | Breaks on UI changes | More Stable        |
| Approach      | Simulates humans     | Executes software  |

------------------------------------------------------------------------

## 🧠 Philosophy

> Websites already expose APIs.
> The UI is just a layer on top.

NoUI removes that layer.

------------------------------------------------------------------------

## 🚀 Quick Start (WIP)

``` bash
git clone https://github.com/your-org/noui
cd noui

uvicorn main:app --reload
```

------------------------------------------------------------------------

## 🔥 Status

Early open source --- expect rough edges.
Contributions welcome.

------------------------------------------------------------------------

## 📢 Closing

Computer-use agents were step one.

**NoUI is what comes next.**

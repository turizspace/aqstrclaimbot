# AQSTR Claim Bot

This folder contains a Python script that automates AQSTR task claiming using a Nostr private key.

## Requirements

Create and activate a virtual environment, then install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Setup

1. Export your Nostr private key as an environment variable:

```bash
export NOSTR_NSEC="nsec1..."
```

2. Optional: set a pre-seeded session cookie if required by your environment:

```bash
export AQSTR_SESSION_COOKIE="your-session-cookie"
```

## Run

```bash
python3 aqstrbot.py
```

## Notes

- This script is for educational purposes only.
- Running it against live services may violate the platform terms of service.
- Use a throwaway key and a test environment.

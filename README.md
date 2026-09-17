# Club Email AI Agent

An AI-powered agent that monitors a Gmail inbox, classifies unread emails
as important or not using Gemini's structured output mode, and applies a
Gmail label to anything flagged important — leaving everything else untouched.

## How it works
- Fetches unread emails via the Gmail API
- Extracts full email body (handles forwarded emails)
- Classifies importance using Gemini (gemini-3.6-flash) with a Pydantic
  schema for structured, typed output
- Applies a Gmail label to important emails only; unimportant emails are
  left completely untouched (still unread, no label)
- Tracks processed message IDs locally to avoid re-classifying on reruns

## Setup
1. Create a virtual environment and install dependencies:
   pip install -r requirements.txt
2. Add your own credentials.json (Google Cloud OAuth client) and a .env
   file with your Gemini API key — neither is included in this repo.
3. Run the script. First run opens a browser for one-time Google OAuth consent.

## Notes
This project is for personal/club use and is not intended to be run against
someone else's Gmail account without their own credentials.

# Imports
import os
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
# ---
import html
import base64
import re
from bs4 import BeautifulSoup
# ---
import json
# ---
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dotenv import load_dotenv
# ---
import time

# Global Configs
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TARGET_LABEL_NAME = "2026-27 Exel Uy"
PROCESSED_IDS_FILE = "processed_ids.json"

rubric = f"""
Mark as important if it:
    "- Is addressed to Exel specifically or requires a personal decision/reply from him
      (can be a general inquiry from a club member regarding Culture Show applications,
      auditions, or logistics)
    - Involves a deadline, task, or responsibility tied to his role as Culture Show Director
    - Comes from a known collaborator (e.g. Sam Cubbage, the Production Coordinator and theater venue contact) —
      any email from a named collaborator should default to important
    - Comes from club officers, university administration, or sponsors,
      regarding show logistics, approvals, or a request that needs a response

    Mark as NOT important if it's a promotional email, newsletter, automated marketing,
    or a general announcement that doesn't need individual action.
"""

# Pydantic Schema (Blueprint for important emails)
# Changed to Batch-Based
class EmailImportance(BaseModel):
    id: str
    is_important: bool = Field(
        description="True if this email is important and requires the Culture Show Director's personal attention; "
                    "False for newsletters, promotions, spam, or general club-wide blasts "
                    "that don't need direct action."
    )
    reason: str = Field(
        description="One short sentence explaining why it was flagged important or not."
    )

class BatchResult(EmailImportance):
    results: list[EmailImportance]

# Functions
def authenticate_gmail(account):
    token_file = f'token_{account}.json'
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Refresh failed ({e}), falling back to full login...")
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0);

        with open(token_file, 'w') as token:
            token.write(creds.to_json())

    service = build('gmail', 'v1', credentials=creds)
    return service

def get_unread_emails(service, max_results=3):
    results = service.users().messages().list(
        userId='me', q='is:unread in:inbox', maxResults=max_results
    ).execute()

    messages = results.get('messages', [])
    if not messages:
        print("No unread messages in your inbox!")
        return []

    email_list = []
    for msg in messages:
        full_msg = service.users().messages().get(
            userId='me', id=msg['id'], format='full'
        ).execute()

        headers = full_msg['payload']['headers']
        sender = html.unescape(next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown'))
        subject = html.unescape(next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject'))
        snippet = html.unescape(full_msg.get('snippet', ''))
        body = extract_email_body(full_msg['payload'])  # NEW

        email_info = {
            'id': msg['id'],
            'sender': sender,
            'subject': subject,
            'snippet': snippet,
            'body': body,  # NEW
        }
        email_list.append(email_info)

    return email_list

def extract_email_body(payload):
    """
    Recursively walks the Gmail message payload to find and decode the body text.
    Prefers text/plain; falls back to stripping HTML from text/html if that's all
    that's available. Returns a plain-text string.
    """
    body_data = None

    def find_part(part):
        nonlocal body_data
        mime_type = part.get('mimeType', '')
        data = part.get('body', {}).get('data')

        if mime_type == 'text/plain' and data:
            body_data = ('plain', data)
            return True  # prefer plain, stop searching once found
        if mime_type == 'text/html' and data and body_data is None:
            body_data = ('html', data)  # keep looking in case plain shows up later

        for sub_part in part.get('parts', []):
            if find_part(sub_part):
                return True
        return False

    find_part(payload)

    if body_data is None:
        return ""

    kind, data = body_data
    decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')

    if kind == 'html':
        decoded = BeautifulSoup(decoded, 'html.parser').get_text(separator=' ', strip=True)

    return decoded.strip()

def load_processed_ids():
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_processed_ids(ids):
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids), f)

def get_or_create_label(service, label_name):
    # Checks if a label exists in Gmail. If it doesn't exist, create a label

    # Fetch all existing user labels
    results = service.users().labels().list(userId='me').execute()
    labels = results.get('labels',[])

    # Check if the label exists
    for label in labels:
        if label['name'].lower() == label_name.lower():
            return label['id']

    # Create the label if it doesn't already exist
    label_object = {
        'name': label_name,
        'labelListVisibility': 'labelShow',
        'messageListVisibility': 'show'
    }

    created_label = service.users().labels().create(
        userId = 'me',
        body = label_object
    ).execute()

    print(f"Created new Gmail label: '{created_label}'")
    return created_label['id']

def apply_label(service, msg_id, label_id):
    # Applies label without touching read status

    body = {'addLabelIds': [label_id]}
    updated_message = service.users().messages().modify(
        userId='me', id=msg_id,body=body
    ).execute()
    print(f"Labeled message {msg_id} as important (left unread status untouched).")
    return updated_message

# New analyze emails function
# Classify email batches in one call instead of one email per call.
def analyze_emails_with_ai(emails, max_retries=3):
    email_blocks = "\n\n".join(
        f"ID: {e['id']}\nSender: {e['sender']}\nSubject: {e['subject']}\nBody: {(e['body'] or '(no body)')[:2000]}"
        for e in emails
    )

    prompt=f"""
    You will classify multiple emails below. For each one, decide if it is IMPORTANT 
    and requires direct, personal attention.

    {rubric}
    

    Return one result per email, matching each result's "id" field exactly to the 
    email's ID below.

    EMAILS:
    {email_blocks}
    """

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchResult,
                    system_instruction="You are a precise email triage assistant screening the Culture Show Director's inbox."
                ),
            )
            return BatchResult.model_validate_json(response.text).results
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 2)
                time.sleep(wait)
            else:
                print(f"Batch classification failed after {max_retries} attempts: {e}")
                return []


'''
def analyze_emails_with_ai(sender, subject, body):
    content = body[:3000] if body else "(no body content available)"

    prompt = f"""
    Determine whether this email is IMPORTANT and requires Exel Uy's direct, personal attention.
    Note: If this is a forwarded email, evaluate the core content and context of the
    original message inside the body, not just the forwarding note on top.

    Mark as important if it:
    - Is addressed to Exel specifically or requires a personal decision/reply from him
      (can be a general inquiry from a club member regarding Culture Show applications,
      auditions, or logistics)
    - Involves a deadline, task, or responsibility tied to his role as Culture Show Director
    - Comes from a known collaborator (e.g. Sam Cubbage, the Production Coordinator and theater venue contact) —
      any email from a named collaborator should default to important
    - Comes from club officers, university administration, or sponsors,
      regarding show logistics, approvals, or a request that needs a response

    Mark as NOT important if it's a promotional email, newsletter, automated marketing,
    or a general announcement that doesn't need individual action.

    Sender: {sender}
    Subject: {subject}
    Body: {content}
    """
    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailImportance,
                system_instruction="You are a precise email triage assistant for Exel Uy, "
                                    "Culture Show Director, screening his inbox for what "
                                    "genuinely needs his attention."
            ),
        )
        return EmailImportance.model_validate_json(response.text)
    except Exception as e:
        print(f"Skipped classification for '{subject}' due to error: {e}")
        return None
'''
def process_inbox_agent(service, max_emails=5):
    label_id = get_or_create_label(service, TARGET_LABEL_NAME)  # fetched ONCE, not per email
    processed_ids = load_processed_ids()
    emails = get_unread_emails(service, max_results=max_emails)

    # If no emails at all (edge case)
    if not emails:
        print("No unread emails to process.")
        return

    # If no new emails
    new_emails = [e for e in emails if e['id'] not in processed_ids]
    if not new_emails:
        print("Nothing new to classify.")
        return

    print(f"{len(emails)} unread, {len(new_emails)} not yet classified.\n")

    results = analyze_emails_with_ai(new_emails, rubric)
    results_by_id = {r.id: r for r in results}

    for email in new_emails:
        analysis = results_by_id.get(email['id'])
        if analysis is None:
            continue

        if analysis.is_important:
            apply_label(service, email['id'], label_id)
            print(f"IMPORTANT: '{email['subject']}' — {analysis.reason}")
        else:
            print(f"Skipped (not important): '{email['subject']}' — {analysis.reason}")

        processed_ids.add(email['id'])

    save_processed_ids(processed_ids)

if __name__ == "__main__":
    load_dotenv()
    client = genai.Client()
    gmail_service = authenticate_gmail('club')
    process_inbox_agent(gmail_service)

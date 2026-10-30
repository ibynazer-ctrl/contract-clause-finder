"""
Contract Clause Finder
----------------------
A beginner-friendly Streamlit app.

What it does:
1. You upload a PDF contract, or paste the text.
2. The app sends that text to Gemini, along with the instructions in prompt.txt.
3. Gemini returns JSON naming the clauses, quoting them, and explaining them.
4. The app checks each quote against the contract, so you can see which quotes
   really appear in the document.

Run it from this folder:
    source venv/bin/activate
    streamlit run app.py

The Gemini API key lives in .env as GEMINI_API_KEY. This file loads that
variable but never prints it.
"""

import html
import io
import json
import os
import re
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pypdf import PdfReader
from pypdf.errors import PdfReadError


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Folder that contains app.py, prompt.txt, and .env.
APP_DIR = Path(__file__).resolve().parent

# Gemini model names. The lite model is only used if the main model stays busy.
PRIMARY_MODEL = "gemini-flash-latest"
BACKUP_MODEL = "gemini-flash-lite-latest"

# After a "high demand" failure, wait 2 seconds, then 4, then 8, and retry.
# That is three retries. If those still fail, we try the lite model once.
RETRY_WAITS_SECONDS = (2, 4, 8)

# Message shown when every retry, including the lite model, is still busy.
BUSY_MESSAGE = "The AI service is busy right now. Please try again in a minute."

# How long the contract text is allowed to be.
MIN_CONTRACT_CHARS = 1_000
MAX_CONTRACT_CHARS = 300_000

# A scanned PDF usually has no text layer, so extraction returns almost nothing.
# Real contracts (even short ones) have far more than this.
SCANNED_PDF_MIN_CHARS = 100

# Curly quotes and apostrophes that should match their straight versions.
# The comment at the right is the character you would see in a document.
CURLY_QUOTE_TABLE = str.maketrans(
    {
        "\u2018": "'",  # ‘ left single quote
        "\u2019": "'",  # ’ right single quote / apostrophe
        "\u201a": "'",  # ‚ low single quote
        "\u201b": "'",  # ‛ high reversed single quote
        "\u2032": "'",  # ′ prime
        "\u201c": '"',  # “ left double quote
        "\u201d": '"',  # ” right double quote
        "\u201e": '"',  # „ low double quote
        "\u201f": '"',  # ‟ high reversed double quote
        "\u2033": '"',  # ″ double prime
        "\u00ab": '"',  # «
        "\u00bb": '"',  # »
    }
)

# Friendly names for the clause ids that prompt.txt asks Gemini to use.
CLAUSE_LABELS = {
    "term_and_renewal": "Term and renewal",
    "position_and_duties": "Position and duties",
    "compensation": "Compensation",
    "termination": "Termination",
    "severance": "Severance",
    "non_compete": "Non-compete",
    "non_solicitation": "Non-solicitation",
    "confidentiality": "Confidentiality",
    "ip_assignment": "Intellectual property",
    "change_in_control": "Change in control",
    "garden_leave": "Garden leave",
    "dispute_resolution": "Dispute resolution",
    "governing_law": "Governing law",
    "definition_of_confidential_info": "Definition of confidential information",
    "exclusions": "Exclusions",
    "permitted_use": "Permitted use",
    "permitted_disclosure": "Permitted disclosure",
    "legally_required_disclosure": "Legally required disclosure",
    "term_and_survival": "Term and survival",
    "return_or_destruction": "Return or destruction",
    "remedies": "Remedies",
    "residuals": "Residuals",
    "standstill": "Standstill",
    "non_solicitation_or_no_contact": "Non-solicitation or no-contact",
    "clean_team": "Clean team",
    "refused_information": "Refused information",
}

DOCUMENT_TYPE_LABELS = {
    "employment_agreement": "Employment agreement",
    "nda": "Non-disclosure agreement (NDA)",
    "other": "Other contract",
}

# prompt.txt uses these three words. "one_way" is shown as "one-way".
DIRECTION_LABELS = {
    "mutual": "Mutual",
    "one_way": "One-way",
    "one-way": "One-way",
    "mixed": "Mixed",
}

VERIFIED_BADGE = "☑️ Verified quote"
NOT_FOUND_BADGE = "⚠️ Quote not found in document"


# ---------------------------------------------------------------------------
# Errors and logging
# ---------------------------------------------------------------------------


class ContractTextError(Exception):
    """The user's text or PDF cannot be analyzed. The message is safe to show."""


class ServiceBusyError(Exception):
    """Gemini stayed overloaded after the retries and the lite-model attempt."""


class ModelResponseError(Exception):
    """Gemini replied, but the reply was not JSON we can display."""


def redact_secrets(text: str) -> str:
    """Remove the API key from a string before it is printed.

    Debugging messages go to the terminal. They must never include the key,
    even if an error object happens to contain it.
    """
    secret = os.getenv("GEMINI_API_KEY") or ""
    if len(secret) >= 8:
        text = text.replace(secret, "[REDACTED]")
    # Google API keys start with "AIza". Hide those too, just in case.
    return re.sub(r"AIza[0-9A-Za-z_\-]{10,}", "[REDACTED]", text)


def log_error_for_debugging(exc: Exception) -> None:
    """Print the error type and message. Do not print the API key."""
    message = getattr(exc, "message", None) or str(exc)
    print(redact_secrets(f"{type(exc).__name__}: {message}"), flush=True)


def is_high_demand_error(exc: Exception) -> bool:
    """True only for HTTP 503 responses that say the model is in high demand."""
    code = getattr(exc, "code", None)
    try:
        code_number = int(code)
    except (TypeError, ValueError):
        code_number = None
    message = f"{getattr(exc, 'message', '')} {exc}".lower()
    return code_number == 503 and "high demand" in message


# ---------------------------------------------------------------------------
# Reading the contract
# ---------------------------------------------------------------------------


def extract_pdf_text(file_bytes: bytes) -> str:
    """Pull the text layer out of a PDF.

    This reads real text stored in the file. It cannot read words that are
    only drawn as a picture (a scan). Scanned files are caught later, when we
    notice that almost no text came out.
    """
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except PdfReadError as exc:
        raise ContractTextError(
            "This PDF could not be read. Please upload a different, text-based PDF."
        ) from exc

    # Some PDFs are marked encrypted even when no password is required.
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ContractTextError(
                "This PDF is password-protected. Please upload an unlocked, text-based PDF."
            ) from exc

    pages = []
    try:
        for page in reader.pages:
            pages.append(page.extract_text() or "")
    except PdfReadError as exc:
        raise ContractTextError(
            "This PDF could not be read. Please upload a different, text-based PDF."
        ) from exc

    return "\n".join(pages).strip()


def prepare_contract_text(raw_text: str, from_pdf: bool) -> str:
    """Check length rules and return the text we will send to Gemini.

    PDFs with almost no text get a different message from pasted text that is
    merely too short. A scan should not be described as "too short".
    """
    text = (raw_text or "").strip()
    readable_chars = sum(1 for character in text if not character.isspace())

    if from_pdf and readable_chars < SCANNED_PDF_MIN_CHARS:
        raise ContractTextError(
            "This PDF has almost no extractable text. It may be a scanned image. "
            "Please upload a text-based PDF."
        )

    if len(text) < MIN_CONTRACT_CHARS:
        raise ContractTextError(
            f"Please provide at least {MIN_CONTRACT_CHARS:,} characters of contract text. "
            f"This one has {len(text):,} characters."
        )

    if len(text) > MAX_CONTRACT_CHARS:
        raise ContractTextError(
            f"This contract is too long ({len(text):,} characters). "
            f"The limit is {MAX_CONTRACT_CHARS:,} characters."
        )

    return text


def load_system_prompt() -> str:
    """Read the instructions Gemini should follow. They live in prompt.txt."""
    prompt_path = APP_DIR / "prompt.txt"
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ModelResponseError(
            "prompt.txt is missing, so the app cannot analyze a contract."
        ) from exc
    if not prompt:
        raise ModelResponseError("prompt.txt is empty, so there are no instructions to send.")
    return prompt


# ---------------------------------------------------------------------------
# Quote verification
# ---------------------------------------------------------------------------


def normalize_for_match(text: str) -> str:
    """Make two pieces of text comparable.

    Before we look for a quote inside the contract, both strings are cleaned
    the same way:
    - curly quotes and apostrophes become straight ones
    - any run of spaces, tabs, or line breaks becomes a single space
    - capital and lowercase letters are treated as the same
    """
    if not text:
        return ""
    cleaned = str(text).translate(CURLY_QUOTE_TABLE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.lower()


def quote_appears_in_contract(exact_quote: str, contract_text: str) -> bool:
    """Return True when the quote is actually in the contract text."""
    if not exact_quote or not str(exact_quote).strip():
        return False
    return normalize_for_match(exact_quote) in normalize_for_match(contract_text)


def clause_was_found(clause: dict) -> bool:
    """Read the found flag. Gemini sometimes returns the words "true"/"false"."""
    found = clause.get("found")
    if isinstance(found, str):
        return found.strip().lower() == "true"
    return found is True


def split_clauses(clauses, contract_text: str):
    """Separate found clauses from missing ones, and check each quote.

    Returns:
        found_clauses: clauses with found == true, each with quote_verified set
        missing_clauses: everything else
        verified_count: how many found quotes really appear in the document
    """
    if not isinstance(clauses, list):
        clauses = []

    found_clauses = []
    missing_clauses = []
    verified_count = 0

    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        if not clause_was_found(clause):
            missing_clauses.append(clause)
            continue

        quote = clause.get("exact_quote")
        quote_text = quote if isinstance(quote, str) else ""
        verified = quote_appears_in_contract(quote_text, contract_text)
        if verified:
            verified_count += 1

        # Copy the clause so we can attach the check result without changing
        # the original JSON from the model.
        annotated = dict(clause)
        annotated["quote_verified"] = verified
        found_clauses.append(annotated)

    return found_clauses, missing_clauses, verified_count


# ---------------------------------------------------------------------------
# Calling Gemini
# ---------------------------------------------------------------------------


def get_client() -> genai.Client:
    """Create a Gemini client using the key in .env.

    load_dotenv() reads GEMINI_API_KEY into the environment. We never print
    that value.
    """
    load_dotenv(APP_DIR / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ModelResponseError(
            "GEMINI_API_KEY is not set. Add it to the .env file and restart the app."
        )
    return genai.Client(api_key=api_key)


def request_model(client: genai.Client, model_name: str, system_prompt: str, contract_text: str):
    """Send one contract to one Gemini model and ask for JSON back.

    system_prompt is prompt.txt. It is passed as the system instruction, not
    mixed into the contract, so the model treats it as the rules to follow.
    response_mime_type tells Gemini to reply with JSON only.
    """
    return client.models.generate_content(
        model=model_name,
        contents=f"Here is the contract to analyze:\n\n{contract_text}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            # 0 asks for the most predictable wording, which helps quotes stay exact.
            temperature=0,
        ),
    )


def call_with_retries(request_fn, waits=RETRY_WAITS_SECONDS):
    """Call the main model, retry when it is in high demand, then try lite.

    request_fn(model_name) performs a single API call.
    waits is the pause before each retry: 2 seconds, then 4, then 8.
    """
    # First try, plus one retry for each wait. With (2, 4, 8) that is 4 calls.
    for attempt in range(len(waits) + 1):
        if attempt > 0:
            time.sleep(waits[attempt - 1])
        try:
            return request_fn(PRIMARY_MODEL)
        except Exception as exc:
            log_error_for_debugging(exc)
            # Other problems (a bad key, a network drop, bad JSON) are not retried.
            if not is_high_demand_error(exc):
                raise

    # The main model was busy every time. Give the smaller model one chance.
    try:
        return request_fn(BACKUP_MODEL)
    except Exception as exc:
        log_error_for_debugging(exc)
        if is_high_demand_error(exc):
            raise ServiceBusyError(BUSY_MESSAGE) from exc
        raise


def parse_model_json(response) -> dict:
    """Turn the model's text into a Python dictionary."""
    raw_text = (getattr(response, "text", None) or "").strip()
    if not raw_text:
        raise ModelResponseError("The AI returned an empty response. Please try again.")

    # response_mime_type should already be raw JSON. This also accepts a reply
    # that was wrapped in ```json fences.
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ModelResponseError(
            "The AI response was not valid JSON. Please try again."
        ) from exc

    if not isinstance(data, dict):
        raise ModelResponseError("The AI response was not a JSON object. Please try again.")
    return data


def analyze_contract(contract_text: str, system_prompt: str) -> dict:
    """Run the full AI step and return the parsed JSON object."""
    client = get_client()

    def request_fn(model_name: str):
        return request_model(client, model_name, system_prompt, contract_text)

    try:
        response = call_with_retries(request_fn)
    except ServiceBusyError:
        raise
    except ModelResponseError:
        raise
    except Exception as exc:
        # call_with_retries already printed this. Show the same type and
        # message on the page, with the API key removed if it ever appears.
        detail = getattr(exc, "message", None) or str(exc)
        safe_message = redact_secrets(f"{type(exc).__name__}: {detail}")
        raise ModelResponseError(safe_message) from exc

    try:
        return parse_model_json(response)
    except ModelResponseError as exc:
        log_error_for_debugging(exc)
        cause = exc.__cause__
        if cause is not None and cause is not exc:
            log_error_for_debugging(cause)
        raise


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def show_text_block(text: str) -> None:
    """Show model-written text without letting it act as HTML or Markdown.

    Contract quotes can contain characters like <, *, or $. Escaping them
    makes those characters show up exactly as they were written.
    """
    lines = html.escape(str(text)).splitlines() or [""]
    body = "<br>".join(lines)
    st.markdown(f'<p class="body-text">{body}</p>', unsafe_allow_html=True)


def show_quote_box(quote: str) -> None:
    """Show the model's quote in a box that looks like a quotation."""
    if not quote or not str(quote).strip():
        show_text_block("The model did not provide a quote for this clause.")
        return
    lines = html.escape(str(quote)).splitlines() or [""]
    body = "<br>".join(lines)
    st.markdown(f'<blockquote class="quote-box">{body}</blockquote>', unsafe_allow_html=True)


def clause_title(clause: dict) -> str:
    """Turn a clause_type id such as "non_compete" into a readable name."""
    clause_type = str(clause.get("clause_type") or "").strip()
    if clause_type in CLAUSE_LABELS:
        return CLAUSE_LABELS[clause_type]
    if not clause_type:
        return "Clause"
    return clause_type.replace("_", " ").capitalize()


def format_parties(parties) -> str:
    """Turn the parties field into one readable line."""
    if isinstance(parties, list):
        names = [str(name).strip() for name in parties if str(name).strip()]
        return ", ".join(names) if names else "Not stated"
    if isinstance(parties, str) and parties.strip():
        return parties.strip()
    return "Not stated"


def format_document_type(document_type) -> str:
    key = str(document_type or "").strip().lower()
    if key in DOCUMENT_TYPE_LABELS:
        return DOCUMENT_TYPE_LABELS[key]
    if not key:
        return "Not stated"
    return key.replace("_", " ").capitalize()


def format_direction(direction) -> str:
    key = str(direction or "").strip().lower()
    if key in DIRECTION_LABELS:
        return DIRECTION_LABELS[key]
    if not key or key == "null":
        return "Not stated"
    return key.replace("_", " ").capitalize()


def render_results(analysis: dict, contract_text: str) -> None:
    """Draw the summary, the found-clause cards, and the not-found list."""
    found_clauses, missing_clauses, verified_count = split_clauses(
        analysis.get("clauses"), contract_text
    )
    found_count = len(found_clauses)

    # The count sits above everything else, as in "12 of 13 quotes verified".
    if found_count:
        count_label = f"{verified_count} of {found_count} quotes verified"
        if verified_count == found_count:
            st.success(count_label)
        else:
            st.warning(count_label)
    else:
        st.info("No clauses from the checklist were found, so there are no quotes to verify.")

    document_type = str(analysis.get("document_type") or "").strip().lower()

    # Document type and parties (and, for an NDA, who has to keep secrets).
    with st.container(border=True):
        if document_type == "nda":
            type_column, party_column, direction_column = st.columns(3)
        else:
            type_column, party_column = st.columns(2)
            direction_column = None

        type_column.markdown("**Document type**")
        type_column.write(format_document_type(document_type))
        party_column.markdown("**Parties**")
        party_column.write(format_parties(analysis.get("parties")))

        # Direction only applies to NDAs. Employment agreements leave it null.
        if direction_column is not None:
            direction_column.markdown("**Direction**")
            direction_column.write(format_direction(analysis.get("direction")))

        st.markdown("**Summary**")
        summary = analysis.get("summary") or "No summary was provided."
        show_text_block(summary)

        if document_type == "nda" and analysis.get("direction_explanation"):
            st.markdown("**Why this direction**")
            show_text_block(analysis.get("direction_explanation"))

    st.divider()

    if found_clauses:
        st.subheader("Clauses found")
        for clause in found_clauses:
            with st.container(border=True):
                st.subheader(clause_title(clause))
                section = clause.get("section")
                if section and str(section).strip() and str(section).strip().lower() != "null":
                    st.caption(str(section))
                if clause.get("quote_verified"):
                    st.markdown(f'<p class="badge badge-ok">{VERIFIED_BADGE}</p>', unsafe_allow_html=True)
                else:
                    st.markdown(
                        f'<p class="badge badge-warn">{NOT_FOUND_BADGE}</p>',
                        unsafe_allow_html=True,
                    )
                quote = clause.get("exact_quote")
                show_quote_box(quote if isinstance(quote, str) else "")
                st.markdown("**Plain English**")
                explanation = clause.get("plain_english") or "No explanation was provided."
                show_text_block(explanation)
    else:
        st.write("None of the checklist clauses were marked as found.")

    # Hidden until the user opens it, so the found clauses stay in front.
    if missing_clauses:
        with st.expander("Not found in this contract", expanded=False):
            for clause in missing_clauses:
                st.markdown(f"**{clause_title(clause)}**")
                explanation = clause.get("plain_english")
                if explanation:
                    show_text_block(explanation)


def run_analysis(contract_text: str) -> None:
    """Ask Gemini about this contract and store the result for the page.

    Streamlit reruns the whole script whenever the user clicks something.
    Saving the result in session_state means we do not call the AI again
    just because they opened the "not found" list.
    """
    st.session_state.pop("analysis", None)
    st.session_state.pop("analyzed_text", None)
    try:
        with st.spinner("Reading the contract and looking for clauses..."):
            system_prompt = load_system_prompt()
            analysis = analyze_contract(contract_text, system_prompt)
    except ServiceBusyError:
        # The retries already printed the underlying error in the terminal.
        st.error(BUSY_MESSAGE)
        return
    except (ModelResponseError, ContractTextError) as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        # Unexpected bugs. Log the type and message, still without the key.
        log_error_for_debugging(exc)
        st.error(
            "Something went wrong while analyzing the contract. "
            "Details were printed in the terminal."
        )
        return

    # prompt.txt tells the model to return {"error": "..."} when the text is
    # not really a contract. Show that sentence instead of clause cards.
    model_error = analysis.get("error") if isinstance(analysis, dict) else None
    if model_error:
        st.error(str(model_error))
        return

    st.session_state["analysis"] = analysis
    st.session_state["analyzed_text"] = contract_text


def inject_styles() -> None:
    """Page styles. The layout itself still uses normal Streamlit widgets."""
    st.markdown(
        """
        <style>
        .block-container { padding-top: 2.2rem; max-width: 920px; }
        h1 { letter-spacing: -0.03em; }
        .body-text { line-height: 1.55; margin-bottom: 0.4rem; }
        .quote-box {
            margin: 0.4rem 0 0.8rem 0;
            padding: 0.85rem 1rem;
            border-left: 4px solid #0f5c6b;
            background: #f4f0e8;
            border-radius: 0 10px 10px 0;
            font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
            line-height: 1.5;
            color: #29241f;
        }
        .badge { font-weight: 600; margin: 0.15rem 0 0.4rem 0; }
        .badge-ok { color: #0f6b45; }
        .badge-warn { color: #8a5a00; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Contract Clause Finder",
        page_icon="📄",
        layout="centered",
        initial_sidebar_state="expanded",
    )
    inject_styles()

    # The disclaimer stays in the sidebar so it is visible while reading results.
    with st.sidebar:
        st.header("Disclaimer")
        st.warning(
            "This tool explains contract language and is not legal advice. "
            "It does not judge whether clauses are enforceable. "
            "Do not upload confidential contracts."
        )
        st.header("How to use it")
        st.markdown(
            "1. Upload a text-based PDF, or paste the contract.\n"
            "2. Click **Find clauses**.\n"
            "3. Read each card. A verified quote was found in your document."
        )
        st.caption(
            f"The text must be between {MIN_CONTRACT_CHARS:,} and {MAX_CONTRACT_CHARS:,} characters."
        )

    st.title("Contract Clause Finder")
    st.write(
        "Upload an employment agreement or NDA, or paste its text. "
        "The app finds the key clauses, quotes them, and explains them in plain English."
    )

    # Two sources, so it is obvious which text will be analyzed.
    source = st.radio(
        "How do you want to add the contract?",
        ["Upload a PDF", "Paste text"],
        horizontal=True,
    )

    from_pdf = source == "Upload a PDF"
    raw_text = ""
    contract_text = ""
    find_clicked = False

    if from_pdf:
        # The file arrives as soon as it is chosen, so we can check it before
        # the user clicks the button.
        uploaded = st.file_uploader(
            "Upload a contract PDF",
            type=["pdf"],
            help="Use a text-based PDF. A scan (a photo of pages) will not work.",
        )
        if uploaded is not None:
            try:
                raw_text = extract_pdf_text(uploaded.getvalue())
            except ContractTextError as exc:
                st.error(str(exc))
        if raw_text.strip():
            try:
                contract_text = prepare_contract_text(raw_text, from_pdf=True)
                st.caption(f"{len(contract_text):,} characters")
            except ContractTextError as exc:
                st.error(str(exc))
        elif uploaded is None:
            st.caption("Choose a PDF to begin.")
        find_clicked = st.button(
            "Find clauses",
            type="primary",
            disabled=not contract_text,
        )
    else:
        # The text box and the button sit in a form so they are submitted
        # together. Otherwise the first click can be lost: Streamlit only
        # saves a text box when it loses focus, which is the click itself.
        with st.form("paste_form", clear_on_submit=False):
            raw_text = st.text_area(
                "Paste the contract text",
                height=280,
                placeholder="Paste the full employment agreement or NDA here.",
            )
            find_clicked = st.form_submit_button("Find clauses", type="primary")

        if raw_text.strip():
            try:
                contract_text = prepare_contract_text(raw_text, from_pdf=False)
                st.caption(f"{len(contract_text):,} characters")
            except ContractTextError as exc:
                st.error(str(exc))
        else:
            st.caption("Paste the contract, then click Find clauses.")

    if find_clicked and contract_text:
        run_analysis(contract_text)
    elif find_clicked and not raw_text.strip():
        st.error("Paste the contract text, or switch to Upload a PDF.")

    # Streamlit reruns this script after the button click. The result is stored
    # above so it stays on the page when the user opens the "not found" list.
    saved_analysis = st.session_state.get("analysis")
    saved_text = st.session_state.get("analyzed_text")
    if saved_analysis and saved_text and saved_text == contract_text:
        st.divider()
        render_results(saved_analysis, saved_text)


if __name__ == "__main__":
    main()

# 📄 Contract Clause Finder

An AI tool that reads an employment agreement or NDA, finds its key clauses, quotes each one exactly, and explains it in plain English — then **checks every quote against the original document** to catch AI mistakes.

**Live demo:** [Try the app](https://contract-clause-finder-ipkedj8a4mwhcvdynd4fv8.streamlit.app)

---

## Why I built this

People sign employment offers and non-disclosure agreements without fully understanding them. Clauses like non-competes, auto-renewals, and IP assignment can affect someone's career for years, but they're written in language most people skip.

I'm studying Cybersecurity & Global Policy at Indiana University and plan to pursue a legal career. This is my second legal-tech project, following my [Privacy Policy Analyzer](https://github.com/ibynazer-ctrl/privacy-policy-analyzer). This time I wanted to tackle a harder problem: making sure an AI's explanation of a legal document is actually grounded in that document.

## What it does

Upload a PDF (or paste text) and the app returns:

- **Document type and parties** — employment agreement or NDA, and who signed it
- **NDA direction** — whether the NDA is **mutual**, **one-way**, or **mixed** (mostly one-way, but with some obligations on both sides), with a one-sentence explanation
- **Clause cards** — for each key clause: the section number, an exact quote, and a plain-English explanation
- **Quote verification** — every quote is marked ✅ verified or ⚠️ not found, with a total at the top (for example, "12 of 13 quotes verified")

### Clauses it looks for

| Employment agreements | NDAs |
|---|---|
| Term & renewal | Definition of confidential information |
| Position & duties | Exclusions |
| Compensation | Permitted use |
| Termination | Permitted disclosure |
| Severance | Legally required disclosure |
| Non-compete | Term & survival |
| Non-solicitation | Return or destruction |
| Confidentiality | Remedies |
| IP assignment | Residuals |
| Change in control | Standstill |
| Garden leave | Non-solicitation / no-contact |
| Dispute resolution | Clean team |
| Governing law | Refused information |

Several of these — **standstill**, **clean team**, and **refused information** — were added after reading real M&A confidentiality agreements during testing, where they turned out to be important.

## Quote verification: checking the AI's work

AI models sometimes paraphrase or invent text, which is a serious problem for a legal tool. In my first project, I fixed this by instructing the AI not to use outside knowledge. Here I went a step further: **the code checks the AI's output instead of trusting it.**

For every clause the AI finds, the app searches the original document for the exact quote. Before comparing, it normalizes both texts (spacing, curly vs. straight quotes, capitalization) so formatting differences from PDF extraction don't cause false alarms. Any quote that can't be found is flagged so the reader knows to check that clause in the original.

## Testing

I tested on four real contracts from public SEC filings (EDGAR):

| Document | Result |
|---|---|
| Executive employment agreement (CEO, investment company, 2015) | 12 of 13 quotes verified |
| Executive employment agreement (COO, same company, same day) | Testing in progress |
| Confidentiality agreement from a pharmaceutical acquisition (2024) | 12 of 12 quotes verified; correctly labeled **mixed** |
| Mutual non-disclosure agreement, biotech company (2022) | 8 of 8 quotes verified; correctly labeled **mutual** |
Highlights from testing:
- It caught an **auto-renewal requiring 90 days' notice**, a **12-month non-compete**, and a **garden leave** provision in the employment agreement.
- In the acquisition NDA, it found an **18-month standstill**, **clean team** access rules, and a clause where the buyer **refused to receive** certain scientific data — and correctly identified the agreement as mostly one-way but with shared confidentiality about the deal talks.
- The one unverified quote (an IP assignment clause) is being investigated to determine whether the AI altered the wording or the PDF text extraction differed.

## Design choices

- **No risk scores.** I originally planned to rate clauses as high or low risk. I removed this because, as a non-lawyer, I couldn't justify those judgments, and ratings can read as legal advice. The tool explains what clauses say; it doesn't judge them.
- **No enforceability claims.** Whether a clause holds up depends on jurisdiction and facts, so the tool never says a clause is enforceable, valid, or illegal.
- **Exact quotes over summaries.** Showing the original language next to every explanation lets readers check the AI themselves.

## How it works

| Part | Technology |
|---|---|
| Interface | [Streamlit](https://streamlit.io) (Python) |
| PDF reading | `pypdf` |
| AI model | Google Gemini via the `google-genai` library, returning structured JSON |
| Analysis instructions | `prompt.txt`, kept separate from the code |
| Reliability | Automatic retries and a backup model when the AI service is busy |
| Hosting | Streamlit Community Cloud |

## Security

- The API key is never in the code. Locally it's in a `.env` file excluded by `.gitignore`; in production it's stored in Streamlit's encrypted secrets.
- The key is bound to a dedicated service account restricted to the Gemini API.
- Users are warned not to upload confidential contracts, since text is sent to a third-party AI provider.

## Run it locally

```bash
git clone https://github.com/ibynazer-ctrl/contract-clause-finder.git
cd contract-clause-finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file with your own [Gemini API key](https://aistudio.google.com):

```
GEMINI_API_KEY=your-key-here
```

Then run:

```bash
streamlit run app.py
```

## Limitations

- **This is not legal advice.** It explains contract language and can make mistakes.
- Scanned PDFs (images of text) can't be read; the PDF must contain selectable text.
- Clause detection is tuned for U.S.-style employment agreements and NDAs.
- Do not upload confidential contracts.

## Future improvements

- Feedback from law faculty on clause coverage and explanations
- Comparing two contracts side by side (for example, two executives at the same company)
- Tracking how a contract changes across amendments
- Highlighting the quoted text inside the original PDF

## Author

**Ibrahim Nazer** — Cybersecurity & Global Policy, Indiana University Bloomington

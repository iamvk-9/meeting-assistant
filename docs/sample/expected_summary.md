# Expected summary for `sample_meeting.mp3`

The recording is a ~100-second scripted kickoff call (Priya, Ravi, Anita). Exact wording varies by LLM,
but a correct run should capture the following.

**Title (generated):** Acme Retail pilot kickoff

**Overview:** Kickoff for the Acme Retail pilot. The team aligned on a two-store launch by November 15,
the technology choice, and the budget, and assigned follow-up actions.

**Key discussion points**
- Pilot launch in two stores by November 15
- PostgreSQL as the data layer, hosted on a single cloud server
- Pilot budget
- Open question: is single sign-on required for store managers?

**Decisions**
- Launch date is November 15
- Use PostgreSQL
- Budget is $12,000

**Risks**
- Payment vendor API limited to 100 requests per minute, which may slow busy stores
- Store managers are only available for training in the first week of November, leaving little schedule slack

**Action items**
| Task | Owner | Due |
|---|---|---|
| Set up the staging environment | Ravi | November 5 |
| Send the revised proposal to Anita | Priya | This Friday |
| Share the sample sales data with Ravi | Anita | Monday |
| Check with IT whether single sign-on is needed for store managers | Anita | (not stated) |

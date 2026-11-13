# Job Radar

A personal job-search dashboard. Every weekday morning it pulls open roles from the public Greenhouse, Lever and Ashby job boards of the companies in `companies.json`. It keeps sales, partnerships and GTM-ops roles in NYC or US-remote, scores them and publishes the results to this site.

## Files you'll edit

- **companies.json**: the companies to watch. Add one with `{"name": "Company", "slug": "their-board-slug"}`. The slug is the last part of their careers URL (for example `jobs.ashbyhq.com/ramp` → `ramp`).
- **status.json**: your application tracker. Set `"Company": "Applied"` (or Recruiter screen, Interviewing, Offer, Passed) and it shows as a tag on the page.

To edit either file, open it on GitHub, click the pencil icon, then **Commit changes**.

## How scoring works (0–100)

- **Role fit:** 50 for mid-market, enterprise or founding AE, partnerships, or deal desk/GTM ops roles. 40 for other AE roles and 35 for account manager roles.
- **Location:** +30 for NYC, +20 for US-remote.
- **Freshness:** +20 if posted in the last 7 days, +10 if in the last 30 days, −30 if older than 90 days.

## Refreshing by hand

Go to **Actions → Update jobs → Run workflow**.

## Office days

The **Office** column is read from each job description. It picks up phrasing like "3 days a week in office", "Tue–Thu in the office", "fully in person" or "remote-first". Hover over a value to see the sentence it came from. If the description doesn't say, the column falls back to the job board's hybrid/on-site/remote label, or shows "Not stated".

Pay ranges appear under the role title when the posting lists one.

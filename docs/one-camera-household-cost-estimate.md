# One-camera household operating-cost estimate

Estimated 31 August 2026. This is gross Google list-price cost before promotional
credits, VAT, hardware, electricity, home internet, development builds, and human
support.

## Bottom line

For one household cooking three times per day, a reasonable planning estimate is
**about DKK 115 per month at the current 2026 Gemini 3.6 Flash price**. The modeled
range is **roughly DKK 55–225 per month**, driven mainly by how many separate false
trigger episodes reach Gemini and how often an inference needs its bounded repair
call.

Google's published non-global Gemini 3.6 Flash rates are USD 0.825 per million
input tokens and USD 4.125 per million output/reasoning tokens through 31 December
2026. The published rates double on 1 January 2027. FoodLog runs the non-global
model in the EU and already accounts at those exact 2026 rates.

The estimate uses the latest available USD/DKK reference rate on 31 August:
**USD 1 = DKK 6.42**, dated 28 August 2026.

## Household scenarios

| Scenario | Cooking episodes/day | False episodes/day | Repair-adjusted Gemini cost/event | Images/month | Month-one estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Quiet kitchen | 3 | 3 | DKK 0.29 cooking / 0.31 false | 3,600 | **DKK 55–60** |
| Expected | 3 | 8 | DKK 0.31 cooking / 0.34 false | 7,650 | **DKK 112–118** |
| Busy / trigger-heavy | 3 | 15 | DKK 0.34 cooking / 0.40 false | 14,400 | **DKK 220–225** |

The expected scenario rounds to **DKK 115/month**:

- 90 cooking inferences × DKK 0.31 = DKK 27.90;
- 240 false-trigger inferences × DKK 0.34 = DKK 81.60;
- image storage, writes, and one backend read of each image ≈ DKK 2.94 in month
  one;
- remaining Cloud Run, Firestore, Pub/Sub, Firebase, and secret traffic should be
  inside their small-usage free allowances; allow DKK 0–5 for shared fixed
  platform residue such as retained container images.

False triggers are not assumed to be free. A person fetching water, a cat on the
counter, or another short kitchen visit still requires the agent to inspect the
event before it can safely decide that no meal should be logged.

## Evidence behind the model-call amount

FoodLog's retained production/evaluation ledger is more useful than a generic
token guess. Successful Gemini 3.6 Flash event runs have commonly cost about
**DKK 0.20–0.34 each**. Representative retained results include DKK 0.200192,
0.248385, 0.273333, 0.288929, 0.317804, 0.328938, and 0.338633. A deliberately
difficult cat negative control needed a primary plus repair call and cost DKK
0.546098. The scenario rates above therefore include increasing repair headroom
rather than pretending every false trigger succeeds in one call.

## Capture and storage assumptions

The expected scenario assumes:

- a cooking session retains about 45 frames: an initial 15-frame burst plus about
  one frame per minute during a 30-minute session;
- a short false trigger retains about 15 frames;
- an average retained JPEG is 1 MiB;
- every retained image is written once and read once from the EU multi-region by
  the Europe backend;
- images are retained indefinitely under the current prototype policy.

For 7,650 new 1 MiB images, month-one image infrastructure is approximately:

| Component | Calculation | DKK |
| --- | --- | ---: |
| EU multi-region replication | 7.47 GiB × USD 0.02/GiB | 0.96 |
| EU multi-region to European regional service | 7.47 GiB × USD 0.02/GiB | 0.96 |
| First-month average storage | 3.74 GiB × USD 0.026/GiB-month | 0.62 |
| Class A object writes | 7,650 × USD 0.01/1,000 | 0.49 |
| Class B object reads | 7,650 × USD 0.0004/1,000 | 0.02 |
| **Total** |  | **3.05** |

Binary-unit rounding accounts for the small difference from the DKK 2.94 model
used in the rounded scenario total; the practical conclusion is unchanged.

Because retained images currently have no expiry, storage accumulates. At the
expected rate, the monthly bill grows by about **DKK 1.25 for each additional
month of retained images**. The expected scenario is therefore roughly DKK
125–130/month by month 12 and about **DKK 1,450 for the first full year** at the
2026 model rate.

## Important current-cadence caveat

The implemented browser and portable-firmware cadence extends the 15-second
one-frame-per-second burst whenever motion remains detected. Sustained cooking
can therefore continue at roughly one uploaded frame per second instead of
falling back to one per minute.

Three 30-minute cooking sessions would then produce about 5,400 cooking frames
per day before false triggers. The current 200-image trial allowance would be
exhausted after only about **3 minutes 20 seconds** of sustained motion. On an
unlimited account, 1 MiB images would add about 158 GiB/month; image-only
storage, replication, operations, and backend reads would add roughly **DKK
60–65 in month one**, while an event containing thousands of images is not a
credible inference workload.

Therefore the DKK 115/month estimate describes the intended bounded cadence
after EVAL-008 calibration, not a guarantee for the current uncalibrated motion
implementation. A hard per-episode frame cap or true transition from burst to
monitoring is required before household economics can be production-validated.

## 2027 price sensitivity

At Google's already-published rates starting 1 January 2027, the Gemini portion
of these figures doubles. If usage behavior is unchanged, the expected household
moves from about **DKK 115/month to about DKK 225/month**, plus the gradual
retained-storage growth described above.

## Pricing sources

- [Google generative AI pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- [Cloud Run pricing](https://cloud.google.com/run/pricing)
- [Firestore pricing](https://cloud.google.com/firestore/pricing)
- [Cloud Storage pricing](https://cloud.google.com/storage/pricing)
- [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing)
- [Secret Manager pricing](https://cloud.google.com/secret-manager/pricing)
- [Frankfurter USD/DKK reference rate](https://api.frankfurter.dev/v1/latest?base=USD&symbols=DKK)

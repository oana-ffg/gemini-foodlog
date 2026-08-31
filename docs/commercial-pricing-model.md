# Commercial subscription price and COGS gate

Prepared 31 August 2026 for one household with one active camera. Prices below
are consumer prices including Danish VAT. Hardware is sold separately.

## Executive summary

**Launch-price hypothesis: DKK 199/month including VAT for one household and one
camera. Do not launch that plan until typical cloud/model COGS is at or below DKK
30/month and total variable operating cost is at or below DKK 40/month.**

At DKK 199, direct web billing and those cost ceilings produce about a 71%
contribution margin after VAT, Stripe Payments plus Stripe Billing, cloud usage,
and a DKK 10 variable support/operations reserve. That margin is before hardware,
salaries, product development, marketing, refunds, chargebacks, and company tax.

The current architecture does not support that price. With the intended bounded
capture cadence, the expected cloud cost is about DKK 115/month through December
2026 and about DKK 225/month from January 2027. Selling the current cost
structure sustainably would require approximately **DKK 599/month now** and
**DKK 1,099/month from January 2027**. Those are engineering-economic prices, not
credible consumer launch recommendations.

## Decision model

The base case assumes:

- 25% Danish VAT, so revenue before VAT is 80% of the displayed consumer price;
- direct web billing with a standard EEA card: 1.5% + DKK 1.80 for Stripe
  Payments, plus 0.7% of billing volume for Stripe Billing;
- a 70% contribution-margin target on revenue before VAT;
- one household and one active camera;
- hardware purchased separately, with no device subsidy in the subscription;
- DKK 10/month of variable support and operations in the optimized case; and
- no app-store commission.

For a monthly consumer price `P` and variable operating cost `C` excluding
payment processing:

```text
net revenue              = P / 1.25
payment and billing fees = 0.022P + 1.80
contribution             = net revenue - fees - C
70% margin cost ceiling  = 0.218P - 1.80
```

At **DKK 199/month**:

| Item | DKK/month |
| --- | ---: |
| Consumer price including VAT | 199.00 |
| Revenue excluding VAT | 159.20 |
| Stripe Payments + Billing | 6.18 |
| Target cloud/model COGS | 30.00 |
| Variable support/operations reserve | 10.00 |
| Contribution | **113.02** |
| Contribution margin on revenue excluding VAT | **71.0%** |

The same formula makes **DKK 41.58/month** the hard total variable-cost ceiling
at a 70% margin. Reserving DKK 10 for support leaves DKK 31.58 for cloud usage;
the operational gate rounds that down to DKK 30.

## Why the current build cannot be priced like a consumer subscription

| Cost state | Cloud/model | Support reserve | Sustainable displayed price at 70% | Practical rounded price |
| --- | ---: | ---: | ---: | ---: |
| Optimized launch target | DKK 30 | DKK 10 | DKK 191.74 | **DKK 199** |
| Current bounded-cadence estimate | DKK 115 | DKK 10 | DKK 581.65 | **DKK 599** |
| Same usage at January 2027 Gemini rates | DKK 225 | DKK 10 | DKK 1,086.24 | **DKK 1,099** |

At DKK 199, the current 2026 cost structure yields only about 18% contribution
margin; at the published January 2027 rates it loses about DKK 82 per household
per month before fixed company costs. Increasing the consumer price cannot rescue
the proposition plausibly; the event-processing cost must fall first.

## Market anchor, not proof of willingness to pay

Current official consumer prices span a wide range:

- Cronometer Gold is USD 10.99/month.
- MyFitnessPal Premium is USD 19.99/month and Premium+ is USD 24.99/month.
- Oura membership is EUR 5.99/month in the EU, in addition to buying the ring.

At the 28 August reference rate of USD 1 = DKK 6.42, MyFitnessPal Premium+ is
about DKK 160 before Danish VAT, or roughly DKK 200 after applying 25% VAT. That
makes DKK 199 a defensible premium hypothesis for genuinely passive food logging,
but it does not prove demand. FoodLog still needs interviews or a paid pilot to
test whether the passive evidence, corrections, and later symptom investigation
are worth that premium.

## Launch gates

Do not offer an unlimited DKK 199 plan until all of these are measured in real
household use:

1. Typical cloud/model COGS is **at most DKK 30/household/month**.
2. A conservative high-usage percentile is **at most DKK 45/month**, with a safe
   entitlement or degraded mode rather than an open-ended bill.
3. Capture cadence has a hard per-episode bound and sustained motion cannot keep
   one-frame-per-second uploads alive indefinitely.
4. Cheap local or first-pass classification prevents obvious water, cat, and
   counter visits from entering the full multi-turn inference workflow.
5. Retention is fixed and priced; raw images and traces are not retained forever.
6. A paid pilot validates willingness to pay, cancellation behavior, and actual
   support burden before annual discounts or hardware subsidies are offered.

## Recommended commercial shape

- **DKK 199/month including VAT:** one household, one active camera, bounded
  retention and usage, hardware purchased separately.
- **No DKK 149 founding plan yet:** that price supports only DKK 30.68 of total
  variable cost at a 70% margin, leaving too little room for both cloud and
  support unless field data proves materially better economics.
- **No unlimited promise:** define a generous ordinary-use entitlement around
  household episodes, not frames or tokens, and fail safely when capture behavior
  is abnormal.
- **No hardware subsidy initially:** the bill of materials, replacements,
  shipping, warranty, and churn are not yet measured.

## Sources and caveats

- [FoodLog one-camera household cost estimate](one-camera-household-cost-estimate.md)
- [Google Cloud generative AI pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- [Danish Tax Agency VAT guidance](https://skat.dk/en-us/erhverv/moms/i-gang-med-moms)
- [Stripe Denmark pricing](https://stripe.com/en-dk/pricing)
- [Cronometer Gold pricing](https://cronometer.com/gold/index.html)
- [MyFitnessPal membership pricing](https://blog.myfitnesspal.com/myfitnesspal-membership-pricing-tiers/)
- [Oura membership pricing](https://support.ouraring.com/hc/en-us/articles/4409086524819-Oura-Membership)
- [Frankfurter USD/DKK reference rate](https://api.frankfurter.dev/v1/latest?base=USD&symbols=DKK)

The 70% target and DKK 10 support reserve are planning assumptions, not observed
FoodLog data. The cost estimate assumes intended bounded capture cadence; current
sustained-motion behavior is not commercially safe. Consumer VAT and payment
rules vary by market. App-store commissions, payment-method mix, refunds,
chargebacks, bad debt, hardware economics, and acquisition cost need separate
models before a real launch.

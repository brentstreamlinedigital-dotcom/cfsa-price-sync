# Supplier Pricelist Request Templates

For each of these 5 suppliers, our automated system can ingest their pricelist
directly if they email us an `.xlsx` or `.csv` attachment with at least these
columns: **`SKU`**, **`Description`**, **`Cost Inc`**, **`Full Retails`**
(any of those exact header names — case-insensitive matching).

Working examples we already receive in this exact format: ARB, Tsunami,
Coldfactor, Frozen, Highon. So this is a normal request — not custom dev work.

Sending email comes **from** your operator address; subject must contain the
phrase **"pricelist"**, **"price list"**, or **"price update"** so our Gmail
poller picks it up.

---

## 1. Dometic (via THR-SA distributor) — 8 products

**Send to:** Your THR-SA account manager (cc dometic.com if relevant)
**Configured to read from:** `thr-outdoor.co.za`, `thrsa.co.za`, `dometic.com`

> Subject: Dometic dealer pricelist request — Camping Fridge SA
>
> Hi [Name],
>
> Could you send us your latest Dometic dealer pricelist in Excel or CSV
> format, with separate columns for **Cost Inc VAT** and **Full Retail
> (RRP)**? Our automated stock-and-price sync expects this layout.
>
> Products we currently stock from you (8): Dometic CFF12, CFF35, CFF45,
> CFF70, CFX35, CFX50, CDF-18, CD-30.
>
> Ideally on a recurring basis (weekly or monthly) so our system stays
> current. The email subject can include "pricelist" so it's auto-routed.
>
> Thanks,
> Brent — Camping Fridge SA

---

## 2. Snomaster — 7 products

**Send to:** Snomaster sales / dealer support
**Configured to read from:** `snomaster.co.za`

> Subject: Snomaster dealer pricelist — Camping Fridge SA
>
> Hi Snomaster team,
>
> Could you send us your current dealer pricelist with **Cost Inc VAT**
> and **RRP** columns, in Excel or CSV? We're set up to receive automated
> price updates from suppliers — this lets us keep all our SKUs in sync
> without manual entry.
>
> Products we stock from you (7): SMDZ-TR42S, SMDZ-LS12, LS25, LS55,
> LS60D, LS135, SMLS-57.
>
> Recurring (monthly is fine) would be appreciated. Subject line just
> needs to contain "pricelist" or "price list".
>
> Thanks,
> Brent — Camping Fridge SA

---

## 3. Flex — 5 products

**Send to:** Flex dealer support
**Configured to read from:** `flexfridge.co.za`, `flexoutdoor.co.za`

> Subject: Flex dealer pricelist — Camping Fridge SA
>
> Hi Flex team,
>
> Could you send a dealer pricelist with **Cost Inc VAT** and **RRP**
> columns? We currently stock CF8, NCF55, FS40, TW75, TW95 from your range.
>
> An xlsx/csv with the standard columns works perfectly — our sync just
> needs SKU, Description, Cost Inc, and Full Retails.
>
> Thanks,
> Brent — Camping Fridge SA

---

## 4. Engel — 3 products

**Send to:** Engel SA dealer support
**Configured to read from:** `engelsa.co.za`, `engel.com.au`

> Subject: Engel dealer pricelist — Camping Fridge SA
>
> Hi Engel SA team,
>
> Could you send your latest Engel dealer pricelist with **Cost Inc VAT**
> and **Full Retail** columns? Currently stocking MR40F-G4NS, MT35F-G3ND-V,
> MT45F-G4ND-V.
>
> A recurring monthly send (xlsx with the standard columns) would be ideal.
>
> Thanks,
> Brent — Camping Fridge SA

---

## 5. DAG (D.A.G) — 1 product

**Send to:** DAG dealer support
**Configured to read from:** `dag.co.za`, `dagsa.co.za`

> Subject: D.A.G dealer pricelist — Camping Fridge SA
>
> Hi D.A.G team,
>
> Could you send your dealer pricelist (xlsx/csv) with **Cost Inc VAT**
> and **Full Retail** columns? We stock the 55L Kalahari Double Door
> Fridge (4X4WA-FR55DDKAL).
>
> Thanks,
> Brent — Camping Fridge SA

---

## When a reply arrives — what happens automatically

1. Gmail poller sees the email (matching `from`-domain + subject keyword).
2. Attachment downloaded and parsed.
3. Cost extracted from the `Cost Inc` column.
4. `master.cost_inc` updated, `cost_source` set to **`supplier`**.
5. Dashboard's "(est.)" badge disappears for that supplier.
6. Margin floor in the pricer now uses **real cost** instead of the estimate.

Until then, the dashboard shows our estimate `(est.)` flagged in amber. The
ratios used (in `config/suppliers/<name>.yaml`):
| Supplier | Cost = RRP × | Implied dealer margin |
|---|---|---|
| Dometic THRSA | 0.72 | 28% |
| Engel | 0.68 | 32% |
| Snomaster | 0.70 | 30% |
| Flex | 0.80 | 20% (their own brand, tighter) |
| DAG | 0.70 | 30% |

Adjust ratios in the YAML if you know your real dealer margin per brand.

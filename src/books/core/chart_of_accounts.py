"""A generic chart of accounts to start a business off.

Deliberately flat: the agent picks one name from a list, and `account_type`
carries the structure that a nested hierarchy would otherwise encode. Parent
categories are still supported (`categories.parent_category_id`) if you later
want sub-accounts.

Descriptions are not decoration — they go into the categorization prompt, so
they are written to tell the agent where the boundaries are.
"""

from __future__ import annotations

from typing import NamedTuple

from books.core.accounting import AccountType


class SeedCategory(NamedTuple):
    name: str
    account_type: AccountType
    description: str


DEFAULT_CHART: tuple[SeedCategory, ...] = (
    # --- Revenue ----------------------------------------------------------
    SeedCategory(
        "Sales Revenue",
        AccountType.REVENUE,
        "Payments from customers for the core product or service.",
    ),
    SeedCategory(
        "Consulting & Services Revenue",
        AccountType.REVENUE,
        "Fees for professional services, retainers and project work.",
    ),
    SeedCategory(
        "Interest Income",
        AccountType.REVENUE,
        "Interest paid to you by a bank or on a note receivable.",
    ),
    SeedCategory(
        "Other Income",
        AccountType.REVENUE,
        "Income that is not from normal operations: rebates, grants, one-offs.",
    ),
    # --- Cost of sales ----------------------------------------------------
    SeedCategory(
        "Cost of Goods Sold",
        AccountType.EXPENSE,
        "Direct cost of what you sold: materials, manufacturing, fulfilment.",
    ),
    SeedCategory(
        "Subcontractors & Freelancers",
        AccountType.EXPENSE,
        "Outside people doing billable client work. Staff go to Payroll.",
    ),
    # --- Operating expenses ----------------------------------------------
    SeedCategory(
        "Payroll & Wages",
        AccountType.EXPENSE,
        "Salaries and wages for employees, including the payroll provider's fee.",
    ),
    SeedCategory(
        "Payroll Taxes",
        AccountType.EXPENSE,
        "Employer-side payroll tax. Income tax on profits goes to Income Tax.",
    ),
    SeedCategory(
        "Employee Benefits",
        AccountType.EXPENSE,
        "Health insurance, retirement contributions, stipends, perks.",
    ),
    SeedCategory(
        "Software & Subscriptions",
        AccountType.EXPENSE,
        "SaaS tools, licences and developer services billed per seat or month.",
    ),
    SeedCategory(
        "Cloud & Hosting",
        AccountType.EXPENSE,
        "Infrastructure billed on usage: AWS, GCP, Cloudflare, CDNs, domains.",
    ),
    SeedCategory(
        "Professional Services",
        AccountType.EXPENSE,
        "Legal, accounting, bookkeeping, tax preparation, advisory.",
    ),
    SeedCategory(
        "Marketing & Advertising",
        AccountType.EXPENSE,
        "Ads, sponsorships, events, content production, brand work.",
    ),
    SeedCategory(
        "Travel",
        AccountType.EXPENSE,
        "Flights, hotels, trains, car hire and ground transport for business.",
    ),
    SeedCategory(
        "Meals & Entertainment",
        AccountType.EXPENSE,
        "Client meals, team meals and events. Often only partly deductible.",
    ),
    SeedCategory(
        "Office Supplies",
        AccountType.EXPENSE,
        "Consumables and small items: stationery, coffee, cables, furniture.",
    ),
    SeedCategory(
        "Equipment & Hardware",
        AccountType.EXPENSE,
        "Laptops, phones, monitors and peripherals expensed rather than capitalised.",
    ),
    SeedCategory(
        "Rent & Utilities",
        AccountType.EXPENSE,
        "Office or coworking rent, electricity, water, waste, cleaning.",
    ),
    SeedCategory(
        "Telecommunications",
        AccountType.EXPENSE,
        "Internet, mobile plans, VoIP and conferencing lines.",
    ),
    SeedCategory(
        "Insurance",
        AccountType.EXPENSE,
        "Liability, professional indemnity, cyber, property and business cover.",
    ),
    SeedCategory(
        "Training & Education",
        AccountType.EXPENSE,
        "Courses, conferences, books and certifications.",
    ),
    SeedCategory(
        "Repairs & Maintenance",
        AccountType.EXPENSE,
        "Fixing or servicing equipment and premises.",
    ),
    SeedCategory(
        "Shipping & Postage",
        AccountType.EXPENSE,
        "Couriers, postage, freight and packaging.",
    ),
    SeedCategory(
        "Bank & Merchant Fees",
        AccountType.EXPENSE,
        "Account fees, wire charges, card processing, FX spread.",
    ),
    SeedCategory(
        "Interest Expense",
        AccountType.EXPENSE,
        "Interest on loans, credit cards and lines of credit — not the principal.",
    ),
    SeedCategory(
        "Taxes & Licences",
        AccountType.EXPENSE,
        "Franchise tax, business licences, registrations, permits.",
    ),
    SeedCategory(
        "Income Tax",
        AccountType.EXPENSE,
        "Corporate or estimated income tax payments.",
    ),
    SeedCategory(
        "Other Expense",
        AccountType.EXPENSE,
        "Genuinely uncategorisable business costs. Prefer a specific category.",
    ),
    # --- Assets -----------------------------------------------------------
    SeedCategory(
        "Accounts Receivable",
        AccountType.ASSET,
        "Money owed to you by customers that has not landed yet.",
    ),
    SeedCategory(
        "Prepaid Expenses",
        AccountType.ASSET,
        "Paid up front for a future period: annual insurance, prepaid rent.",
    ),
    SeedCategory(
        "Fixed Assets",
        AccountType.ASSET,
        "Capitalised purchases depreciated over time rather than expensed now.",
    ),
    SeedCategory(
        "Inventory",
        AccountType.ASSET,
        "Goods bought or made and held for sale.",
    ),
    SeedCategory(
        "Transfers Between Accounts",
        AccountType.ASSET,
        "Movement between accounts you own. Neither income nor expense — use "
        "this for both sides so the pair nets to zero.",
    ),
    # --- Liabilities ------------------------------------------------------
    SeedCategory(
        "Accounts Payable",
        AccountType.LIABILITY,
        "Bills you have received and not yet paid.",
    ),
    SeedCategory(
        "Credit Card Payable",
        AccountType.LIABILITY,
        "Balance owed on a company card. Payments to the card reduce this.",
    ),
    SeedCategory(
        "Loans Payable",
        AccountType.LIABILITY,
        "Principal on loans and lines of credit. Interest goes to Interest Expense.",
    ),
    SeedCategory(
        "Sales Tax Payable",
        AccountType.LIABILITY,
        "Sales tax or VAT collected from customers and owed to the authority.",
    ),
    SeedCategory(
        "Accrued Liabilities",
        AccountType.LIABILITY,
        "Costs incurred but not yet invoiced or paid.",
    ),
    # --- Equity -----------------------------------------------------------
    SeedCategory(
        "Owner Contribution",
        AccountType.EQUITY,
        "Money the owner put into the business.",
    ),
    SeedCategory(
        "Owner Draw",
        AccountType.EQUITY,
        "Money the owner took out. Not payroll and not an expense.",
    ),
    SeedCategory(
        "Retained Earnings",
        AccountType.EQUITY,
        "Accumulated profit left in the business across prior periods.",
    ),
)


def chart_summary() -> dict[str, int]:
    """How many seed categories of each type — used by the CLI and tests."""
    counts: dict[str, int] = {}
    for entry in DEFAULT_CHART:
        counts[entry.account_type.value] = counts.get(entry.account_type.value, 0) + 1
    return counts

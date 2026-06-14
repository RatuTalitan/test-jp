# Implementation Plan: Cotton Seed Oil Cake Marketplace

## Overview

This plan converts the design into incremental, test-driven coding steps for the Python 3.11 / python-telegram-bot / PostgreSQL modular monolith. It builds strictly bottom-up: scaffolding → config/secrets → schema/migrations → versioned domain models → domain services (Auth, Catalog, Cart, Order + state machine, Payment, Fulfillability, Notification, Admin_Console) → Bot_Interface (long-poll `UpdateSource` + `MessagingChannel` adapter, voice baseline) → final wiring.

Each implementation sub-task is paired with the property-based tests (Hypothesis, `max_examples >= 100`, each test tagged `Feature: cottonseed-oilcake-marketplace, Property N: ...`) and example/integration tests called for in the design's Testing Strategy. Every Correctness Property P1a–P33 is implemented by exactly one property test. The global no-oversell invariant (P15) uses the model-based testing approach from the design (a reference stock model compared against the implementation after randomized operation sequences).

All services are written against in-memory or transactional-test repositories so property tests run fast without external calls, and all Telegram-specific code stays behind the `MessagingChannel` / `UpdateSource` seams so Phase 2 channels remain cheap.

## Tasks

- [x] 1. Project scaffolding and tooling
  - [x] 1.1 Set up the Python 3.11 project skeleton and test harness
    - Create the package layout: `src/marketplace/` with empty module packages `bot_interface/`, `auth/`, `catalog/`, `cart/`, `order/`, `payment/`, `fulfillability/`, `notification/`, `admin/`, `domain/`, `db/`, `config/`; plus `tests/` (with `tests/properties/` and `tests/integration/`)
    - Add dependency manifest (`pyproject.toml`/`requirements.txt`) pinning python-telegram-bot v21+, SQLAlchemy + psycopg, Alembic, Hypothesis, pytest, pytest-asyncio
    - Configure pytest (markers for `property`, `integration`, `smoke`) and a Hypothesis profile with `max_examples=100` as the floor
    - _Requirements: 13.1, 13.2 (foundation)_

- [x] 2. Configuration and secrets handling
  - [x] 2.1 Implement environment-based configuration loader
    - Implement a `config` module that reads `BOT_TOKEN`, `DB_URL`, `OBJECT_STORE_KEY`, `SELLER_TELEGRAM_ID`, `WEBHOOK_SECRET_TOKEN`, `UPI_ADDRESS`, and the `verified_contact` encryption key from environment variables / host secret manager only
    - Fail fast with a clear error if a required secret is missing; never log secret values
    - Add a committed `.env.example` documenting key names only (no values)
    - _Requirements: 12.5_
  - [ ]* 2.2 Add CI secret-scanning smoke check
    - Add a CI workflow/script that scans the repo for committed credentials and asserts only `.env.example` (names, no values) is present
    - _Requirements: 12.5_
  - [x] 2.3 Implement the centralized branding/strings module
    - Implement a centralized branding/strings module holding the product name "Jan Purna", the Devanagari form "जन पूर्णा", the short name / monogram "JP", and taglines, so all bot copy references it (and a future WhatsApp / web channel can reuse it) rather than hard-coding brand strings across handlers; used by the `/start` welcome and onboarding messaging
    - Keep all brand strings configurable from this single module so updates do not touch domain services or stored data (per the design's "Branding" subsection)
    - _Requirements: 1.1, 1.5_
  - [x] 2.4 Implement the centralized Message_Catalog (i18n) module
    - Implement a presentation-layer `Message_Catalog` (shared `i18n` module inside the Bot_Interface seam) that holds **complete Hindi AND English entries for every user-facing string** (both languages fully populated — every prompt, confirmation, error, notification, and button label exists in both Hindi and English) and resolves strings by a stable string key plus the **active language code**, where the active language is the user's persisted preference and **defaults to Hindi when the preference is unset/NULL**; no user-facing copy is hard-coded anywhere in handlers (per the design's "Language and Localization" subsection)
    - Provide graceful fallback: if a key is ever missing/untranslated for the active language, the lookup resolves to the **other supported language, preferring Hindi**, and the flow continues without blocking, aborting, or suspending; brand strings ("Jan Purna", "जन पूर्णा", "JP") are language-independent and rendered identically regardless of active language
    - Make the catalog a versioned asset carrying a `format_version`, additive-only (new keys/languages never break older payloads); domain services (Auth, Catalog, Cart, Order, Payment, Notification, Admin) stay language-agnostic and return stable status/error codes (e.g., `QTY_BELOW_MOQ`, `DUPLICATE_UTR`, `NOT_AUTHORIZED`) plus data placeholders that the Bot_Interface maps to localized templates via the catalog
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.9_
  - [ ]* 2.5 Write property test for message catalog lookup and Hindi fallback
    - **Property 35: Message catalog lookup always resolves with Hindi fallback**
    - Asserts active-language resolution, Hindi default for an unset/NULL preference, fallback to the other supported language preferring Hindi, brand-string language-independence ("Jan Purna", "जन पूर्णा", "JP" identical in both languages), and the persisted-preference round-trip (set language → subsequent lookups resolve in the selected language)
    - **Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8, 18.9**
  - [ ]* 2.6 Write example tests for default-Hindi and English-when-selected rendering
    - Verify content resolves in Hindi by default (18.1) and only resolves in English when English is the selected active language (18.5), using representative string keys
    - _Requirements: 18.1, 18.5_

- [x] 3. Database schema and migrations
  - [x] 3.1 Create initial migration: meta, enums, users, categories, products
    - Add `meta` table holding `schema_version`; define `order_state`, `unit`, `role` enums
    - Create `users` (channel-neutral `user_id` PK, unique `telegram_user_id`, encrypted `verified_contact`, `contact_verified_at`, `role`, `offline_payment_allowed` default false, and `language_preference ENUM('HI','EN') NULL` — a NULL/unset value means no stored preference and is treated as the Hindi default by the renderer)
    - Create `categories` (case-insensitive unique name 1–50) and `products` with CHECK constraints: price ∈ [0, 9999999.99], MOQ ∈ (0, 9999999], stock ∈ [0, 9999999], and the defense-in-depth `CHECK (stock_quantity >= 0)`; add browsing indexes
    - _Requirements: 1.6, 2.5, 2.6, 2.7, 2.8, 2.9, 13.7, 17.1, 18.2, 18.4, 18.5_
  - [x] 3.2 Create migration: carts, cart_items, orders, order_items
    - `carts` (one per customer) and `cart_items` with `UNIQUE(cart_id, product_id)` to enforce combine-on-add
    - `orders` (`order_id`, unique `order_number`, `state`, `total_amount`, `rejection_reason`, `created_at`, `schema_version`) and `order_items` (snapshot `unit_price`, `line_amount`); add `(customer_id, created_at DESC)`, `(state, created_at)`, `(product_id)` indexes
    - _Requirements: 4.5, 5.1, 5.2, 7.5, 10.1, 10.6, 13.7, 16.1_
  - [x] 3.3 Create migration: payments, audit_trail, notifications
    - `payments` (`order_id` unique, `utr TEXT UNIQUE NULL` — variable length to support the configurable UTR pattern, `utr_submitted_at`, `screenshot_object_key`)
    - `audit_trail` (append-only: grant INSERT/SELECT only to the app role; `action` enum, `detail` JSONB with `format_version`, `acting_user_id`, `created_at`)
    - `notifications` (`kind`, `transition_seq`, `payload` JSONB, `status`, `attempts`, `last_attempt_at`, `next_attempt_at`, `UNIQUE(order_id, kind, transition_seq)`, `(status, next_attempt_at)` index)
    - _Requirements: 6.3, 6.4, 11.3, 13.7, 15.12, 17.8_
  - [ ]* 3.4 Write migration apply/round-trip integration test
    - Apply all migrations to a transactional test DB; assert constraints (UTR uniqueness, stock CHECK, audit append-only privilege) are enforced
    - _Requirements: 6.4, 7.4, 13.7, 15.12_
  - [x] 3.5 Create migration: seller_settings single-row table
    - Add a `seller_settings` table holding the Seller-configured values: `pickup_location` (TEXT, 1–500 chars), `upi_address` (TEXT, VPA-validated), `upi_qr_object_key` (TEXT, object-storage reference), `utr_pattern` (TEXT, default `'^[A-Za-z0-9]{12}$'`), `updated_at` (TIMESTAMPTZ), and `schema_version` (SMALLINT DEFAULT 1)
    - Enforce a singleton constraint so at most one settings row can exist (e.g., a fixed single-row primary key / `CHECK` on a constant id); seed nullable pickup/UPI values and the default `utr_pattern`
    - _Requirements: 6.2, 6.6, 19.1, 19.3, 19.5, 19.7_

- [x] 4. Domain models and versioned serialization
  - [x] 4.1 Implement domain entities and repository interfaces
    - Define dataclasses/types for User, Category, Product, Cart/CartItem, Order/OrderItem, Payment, AuditEntry, Notification, plus typed result objects (Rejected/NotFound/NotAuthorized/Unauthenticated/Conflict) used across services
    - Define repository protocols with both an in-memory implementation (for property tests) and a SQLAlchemy implementation (for runtime), keeping services free of direct DB poking
    - _Requirements: 13.7, 13.8_
  - [x] 4.2 Implement versioned serialization for all domain formats
    - Add serialize/deserialize for every entity embedding `format_version` (and `schema_version` on order rows), additive-only; JSON payloads for notifications and audit details carry `format_version: 1`
    - _Requirements: 13.7_
  - [ ]* 4.3 Write property test for versioned round-trip
    - **Property 32: Domain data formats round-trip across versions**
    - **Validates: Requirements 13.7**
  - [x] 4.4 Implement the monetary rounding utility (Monetary_Rounding)
    - Implement a shared rounding utility using Python `decimal.Decimal` with `ROUND_HALF_UP` (never binary `float`): `line_amount = round(quantity × unit_price, 2)` half-up, and `total = sum of the already-rounded line amounts` (round each line first, then add) so displayed line amounts always sum exactly to the total
    - Convert `unit_price` and `quantity` to `Decimal` before multiplication and quantize the result to two places; this utility is the single source used by Cart totals, Order placement amounts, and modification recalculation
    - _Requirements: 4.9, 5.2, 15.11_

- [x] 5. Checkpoint - persistence foundation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement Auth_Service
  - [x] 6.1 Implement contact registration, identification, roles, offline flag, and language preference
    - `register_contact` stores Verified_Contact iff shared contact `user_id` == sender `user_id` (single all-or-nothing transaction so a storage failure leaves no partial identity); `identify` returns stable returning users; `role_of` returns ADMIN only for `SELLER_TELEGRAM_ID`; `require_admin`; `set_offline_payment_allowed`
    - Add `set_language_preference(user_id, 'HI' | 'EN')` that persists the user's selected presentation language to `users.language_preference` (migration 3.1) for both Customers and the Seller — the small write the Bot_Interface language-switch control (task 19.6) invokes; an unset/NULL value is treated as the Hindi default by the renderer
    - _Requirements: 1.2, 1.3, 1.6, 1.7, 1.8, 1.9, 12.1, 17.1, 17.3, 18.4, 18.5, 18.8_
  - [ ]* 6.2 Write property test for contact storage on matching identity
    - **Property 1a: Verified contact stored only on matching identity**
    - **Validates: Requirements 1.2, 1.8**
  - [ ]* 6.3 Write property test for role assignment and admin gating
    - **Property 1b: Role assignment and admin gating**
    - **Validates: Requirements 1.6, 1.7, 12.1**
  - [ ]* 6.4 Write property test for stable returning-user identification
    - **Property 2: Returning user identification is stable**
    - **Validates: Requirements 1.3**
  - [ ]* 6.5 Write example tests for auth edge cases
    - Storage-failure atomicity via fault injection (1.9); contact-mismatch rejection re-presents Share Contact (1.8)
    - _Requirements: 1.8, 1.9_
  - [ ]* 6.6 Write example tests for language-preference persistence and round-trip
    - Verify `set_language_preference` persists `users.language_preference` for both a Customer and the Seller, that a subsequent identify/read returns the stored language (round-trip), and that an unset/NULL preference resolves to the Hindi default
    - _Requirements: 18.4, 18.5, 18.8_

- [x] 7. Implement Catalog_Service
  - [x] 7.1 Implement product create/update with validation
    - `create_product`/`update_product` enforcing ranges (price, MOQ, stock), name 1–100, description ≤1000, unit ∈ {kilogram, quintal, bag}, required-field checks; allow stock = 0 products to exist
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.7, 2.9, 2.10_
  - [x] 7.2 Implement category creation with case-insensitive uniqueness
    - `create_category` rejecting duplicate (case-insensitive) names
    - _Requirements: 2.8, 2.11_
  - [x] 7.3 Implement availability, browsing, and product detail
    - `set_availability`, `list_available_grouped_by_category`, `list_available_in_category`, `get_product`; exclude unavailable products; render availability indicator ("in stock" when stock > 0, "out of stock" when stock == 0); empty-catalog and empty-category messages
    - _Requirements: 2.4, 3.1, 3.2, 3.4, 3.5, 3.6_
  - [ ]* 7.4 Write property test for catalog field validation
    - **Property 3: Catalog field validation rejects out-of-range submissions**
    - **Validates: Requirements 2.5, 2.6, 2.9, 2.10**
  - [ ]* 7.5 Write property test for product create/update round-trip
    - **Property 4: Product create/update round-trip**
    - **Validates: Requirements 2.1, 2.3, 2.7**
  - [ ]* 7.6 Write property test for category name uniqueness
    - **Property 6: Category name uniqueness**
    - **Validates: Requirements 2.8, 2.11**
  - [ ]* 7.7 Write property test for browsing returns exactly available products
    - **Property 5: Browsing returns exactly the available products**
    - **Validates: Requirements 2.4, 3.1, 3.4**
  - [ ]* 7.8 Write property test for product detail and availability indicator
    - **Property 7: Product detail and availability indicator**
    - **Validates: Requirements 3.2**
  - [ ]* 7.9 Write example tests for empty catalog/category responses
    - No-available-products message (3.5); no-products-in-category message (3.6)
    - _Requirements: 3.5, 3.6_

- [x] 8. Implement Cart_Service
  - [x] 8.1 Implement cart add/change/remove/view with validation and totals
    - `add_item` (combine-on-add then re-validate), `change_qty`, `remove_item`, `view`, `clear`; validate qty > 0, qty ≥ MOQ, qty ≤ current stock; reject adding zero-stock products; compute line total and cart total using the shared `Monetary_Rounding` utility (task 4.4): each line total = round(qty × price, 2, half-up) and cart total = Σ rounded line totals
    - _Requirements: 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_
  - [ ]* 8.2 Write property test for zero-stock products never addable
    - **Property 8: Zero-stock products are never addable**
    - **Validates: Requirements 3.3**
  - [ ]* 8.3 Write property test for cart quantity validation
    - **Property 9: Cart quantity validation**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7**

- [x] 9. Checkpoint - catalog and cart
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement Order_Service core and state machine
  - [x] 10.1 Implement the guarded transition function and allowed-transition table
    - Single `transition(order, event, actor, guards)` consulting the allowed-transition table; every illegal (state, event, actor) leaves state unchanged; no transition leaves COMPLETED/CANCELLED/REJECTED; auto PLACED → PAYMENT_PENDING edge
    - _Requirements: 6.7, 7.2, 7.7, 8.1, 8.3, 8.4, 8.5, 8.7, 8.8, 9.1, 9.4_
  - [x] 10.2 Implement order placement
    - `place_order` rejects empty cart, over-stock lines, and unauthenticated customers (no Verified_Contact); on success copies cart lines with snapshot `unit_price`, computes per-line amounts and the order total via the shared `Monetary_Rounding` utility (task 4.4, round-each-line-then-sum), assigns unique id, records customer/timestamp, ends in PAYMENT_PENDING, clears the cart, presents UPI instructions, and enqueues the Seller new-order notification
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 12.6_
  - [ ]* 10.3 Write property test for order placement copy/unique-id/clear-cart
    - **Property 11: Order placement copies the cart, assigns a unique id, and clears the cart**
    - **Validates: Requirements 5.1, 5.2, 5.5, 5.6**
  - [ ]* 10.4 Write property test for placement rejection cases
    - **Property 12: Placement is rejected for empty carts, over-stock lines, or unauthenticated users**
    - **Validates: Requirements 5.3, 5.4, 12.6**
  - [ ]* 10.5 Write property test for cart and order totals
    - **Property 10: Cart and order totals equal the sum of line totals**
    - Totals use the `Monetary_Rounding` utility (round each line to two decimals half-up, then sum the rounded line amounts)
    - **Validates: Requirements 4.9, 5.2**
  - [ ]* 10.6 Write property test for the allowed transition graph
    - **Property 14: Order-state transitions follow only the allowed graph**
    - **Validates: Requirements 6.7, 7.2, 7.7, 8.1, 8.3, 8.4, 8.5, 8.7, 8.8, 9.1, 9.4**

- [x] 11. Implement Payment_Service
  - [x] 11.1 Implement UPI presentation and UTR submission with uniqueness
    - `present_instructions` reads the **Seller-configured** values from `seller_settings` (current `upi_address`, the static QR via `upi_qr_object_key`, plus the order's amount due) rather than env config; `submit_utr` validates the submitted UTR against the **configured** `seller_settings.utr_pattern` (default exactly 12 alphanumeric, not a hardcoded rule) while in PAYMENT_PENDING, enforces global UTR uniqueness via the DB constraint (uniqueness behavior unchanged; `payments.utr` is TEXT/variable length), and transitions to PAYMENT_SUBMITTED; enqueues Seller verification notification
    - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 19.7_
  - [x] 11.2 Implement optional screenshot attachment to object storage
    - `attach_screenshot` validates supported format and ≤10MB, stores blob in the private object-storage bucket, and saves only `screenshot_object_key` in the DB
    - _Requirements: 6.3, 6.8_
  - [ ]* 11.3 Write property test for UTR validation and uniqueness
    - **Property 13: UTR validation and uniqueness**
    - Validate against the configured `utr_pattern` (default 12 alphanumeric); uniqueness enforced across orders
    - **Validates: Requirements 6.2, 6.4, 6.6**
  - [ ]* 11.4 Write example tests for screenshot format/size boundaries
    - Random sizes/formats around the 10MB and format boundaries (6.3/6.8)
    - _Requirements: 6.3, 6.8_

- [x] 12. Implement atomic approval stock decrement and concurrency safety
  - [x] 12.1 Implement atomic stock decrement at Payment Verified → Approved
    - In one transaction: lock involved product rows with `SELECT ... FOR UPDATE` ordered by `product_id`, re-read stock, decrement each line iff every line qty ≤ current stock, set APPROVED, notify Customer; otherwise stay PAYMENT_VERIFIED, change no stock, notify Seller with affected lines and available stock
    - _Requirements: 7.2, 7.3, 7.4, 7.8, 16.5_
  - [x] 12.2 Implement payment rejection and remaining fulfillment transitions
    - `reject` from PAYMENT_SUBMITTED requires a 1–500 char reason and records it; implement mark_ready (APPROVED → READY_FOR_PICKUP) and mark_collected (READY_FOR_PICKUP → COMPLETED) with their notifications
    - _Requirements: 7.5, 7.6, 7.9, 8.1, 8.2, 8.3, 8.6_
  - [ ]* 12.3 Write property test for exact approval decrement
    - **Property 16: Approval decrements stock by exactly the ordered quantities**
    - **Validates: Requirements 7.3**
  - [ ]* 12.4 Write property test for payment rejection reason validation
    - **Property 17: Payment rejection requires a valid reason**
    - **Validates: Requirements 7.5, 7.6**
  - [ ]* 12.5 Write model-based property test for the no-oversell invariant
    - **Property 15: Stock is never oversold (global safety invariant)**
    - Use the model-based approach: a reference model tracks expected stock as `initial/edited − Σ approved deductions`; apply randomized interleaved approval/modification/cancel/edit sequences against a transactional test DB and compare after each operation
    - **Validates: Requirements 7.3, 7.4, 16.5**

- [x] 13. Implement Fulfillability Engine
  - [x] 13.1 Implement fulfillability computation and stock-change recompute
    - Pure `is_fulfillable` and `shortfalls`; recompute for every not-yet-Approved order containing a product whose stock changed (approval decrement, modification/cancellation return, or Seller stock edit); computed on read, never stored
    - _Requirements: 16.1, 16.2_
  - [ ]* 13.2 Write property test for fulfillability correctness and flagging
    - **Property 21: Fulfillability is computed correctly and flagged consistently**
    - **Validates: Requirements 16.2, 16.3, 16.4**

- [x] 14. Implement order cancellation and status tracking
  - [x] 14.1 Implement customer cancellation
    - `cancel` succeeds only when the actor is the placing customer and the order is in Placed/Payment Pending/Payment Submitted; sends customer confirmation and notifies Seller; rejects unauthorized actors and non-cancellable states leaving state unchanged
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_
  - [x] 14.2 Implement order status queries with privacy
    - `list_customer_orders` (descending by created_at), `get_order_for_customer` (ownership-enforced, non-disclosing error for others), `list_active_for_seller` (non-terminal, ascending), order detail with line items/total/state/payment reference, "not yet provided" placeholder when no UTR, and no-orders message
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 12.2_
  - [ ]* 14.3 Write property test for cancellation authorization
    - **Property 18: Cancellation authorization**
    - **Validates: Requirements 9.1, 9.4, 9.5**
  - [ ]* 14.4 Write property test for history ordering and privacy
    - **Property 19: Order history and active-order ordering and privacy**
    - **Validates: Requirements 10.1, 10.5, 10.6, 12.2**
  - [ ]* 14.5 Write property test for payment-reference placeholder
    - **Property 20: Payment reference placeholder**
    - **Validates: Requirements 10.4**

- [x] 15. Checkpoint - order lifecycle core
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Implement Seller order modification with stock-delta adjustment
  - [x] 16.1 Implement modification in pre-decrement states
    - `modify` (add/remove/change line) restricted to Pre_Fulfillment_State and to the Seller; validate resulting qty > 0 and ≥ MOQ; in PLACED/PAYMENT_PENDING/PAYMENT_SUBMITTED/PAYMENT_VERIFIED validate against current stock without decrementing; reject reducing to zero line items; newly added lines snapshot the Product's **current catalog price per Unit at modification time** while existing untouched lines keep their **original snapshot `unit_price`**; recompute each line amount from that line's snapshot `unit_price` and the order total via the shared `Monetary_Rounding` utility (task 4.4); leave state unchanged; not-found and authorization handling
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.10, 15.11, 15.13, 15.14, 15.15, 15.16_
  - [x] 16.2 Implement approved-state modification with stock delta and audit entry
    - For APPROVED orders adjust product stock by exactly (old − new) under `FOR UPDATE` locking — return stock on reduce/remove, deduct on increase, reject increases beyond available stock with nothing changed; newly added lines snapshot the Product's current catalog price at modification time while existing untouched lines retain their original snapshot `unit_price`, and recalculation uses each line's snapshot `unit_price` via the `Monetary_Rounding` utility (task 4.4); append exactly one Audit_Trail entry and notify the customer
    - _Requirements: 15.7, 15.8, 15.9, 15.11, 15.12, 15.13, 15.14, 15.15, 15.16_
  - [ ]* 16.3 Write property test for modification authorization, state guard, minimum-line
    - **Property 22: Modification authorization, state guard, and minimum-line invariant**
    - **Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.10**
  - [ ]* 16.4 Write property test for modification quantity validation
    - **Property 23: Modification quantity validation**
    - **Validates: Requirements 15.5, 15.6, 15.8**
  - [ ]* 16.5 Write property test for approved-state delta stock adjustment
    - **Property 24: Approved-state modification adjusts stock by the exact delta**
    - **Validates: Requirements 15.7, 15.8, 15.9**
  - [ ]* 16.6 Write property test for modification totals and state preservation
    - **Property 25: Modification recomputes totals and preserves state**
    - Recalculation uses each line's snapshot `unit_price` (new lines snapshot current catalog price; untouched lines keep original) rounded via `Monetary_Rounding`
    - **Validates: Requirements 15.11, 15.14, 15.15, 15.16**

- [x] 17. Implement offline-payment path and audit trail
  - [x] 17.1 Implement the offline approval edge and offline approval audit entry
    - Add the guarded PAYMENT_PENDING → PAYMENT_VERIFIED edge permitted iff the customer's `offline_payment_allowed` is true (no UTR required, never auto-approved); standard stock-guarded Payment Verified → Approved follows; flagged customers may still optionally submit a valid UTR; on approval with no recorded UTR append exactly one offline-exception Audit_Trail entry; non-flagged customers can never reach Approved without a UTR
    - _Requirements: 8.9, 17.4, 17.5, 17.6, 17.7, 17.8_
  - [ ]* 17.2 Write property test for offline orders never auto-approving
    - **Property 27: Offline-payment orders never auto-approve and respect the flag**
    - **Validates: Requirements 8.9, 17.4, 17.5, 17.7**
  - [ ]* 17.3 Write property test for offline flag management round-trip and authorization
    - **Property 28: Offline flag management round-trip and authorization**
    - **Validates: Requirements 17.1, 17.2, 17.3, 17.6**
  - [ ]* 17.4 Write property test for append-only audit on modification and offline approval
    - **Property 26: Significant Seller actions append exactly one audit entry (append-only)**
    - **Validates: Requirements 15.12, 17.8**

- [x] 18. Implement Notification_Service and retry worker
  - [x] 18.1 Implement transactional notification enqueue
    - `enqueue` writes a PENDING `notifications` row in the same transaction as the state change, with idempotent `(order_id, kind, transition_seq)`; wire enqueue into every state transition and Seller event
    - _Requirements: 11.1, 11.2_
  - [x] 18.2 Implement the retry worker delivery loop
    - `deliver_pending` polls due rows (`status PENDING/FAILED AND next_attempt_at ≤ now`), delivers via Bot_Interface; on success mark DELIVERED and mark any prior failure resolved; on failure record failure with order id + recipient, increment attempts, set `next_attempt_at = now + 30s`; after 4 total attempts mark UNDELIVERED leaving order state unchanged
    - _Requirements: 11.3, 11.4, 11.5_
  - [ ]* 18.3 Write property test for transition-enqueues-notification
    - **Property 29: Every state transition enqueues a customer notification**
    - **Validates: Requirements 11.1**
  - [ ]* 18.4 Write property test for bounded retry without state corruption
    - **Property 30: Notification retry is bounded and never corrupts order state**
    - **Validates: Requirements 11.3, 11.4, 11.5**
  - [ ]* 18.5 Write example tests for notification content
    - Seller/customer message content for new order, payment ready, approval, rejection+reason, ready-for-pickup, completion, cancellation, modification (5.7, 6.5, 7.8, 7.9, 8.2, 8.6, 9.3, 9.6, 11.2, 15.13) using a mocked `MessagingChannel`
    - _Requirements: 5.7, 6.5, 7.8, 7.9, 8.2, 8.6, 9.3, 9.6, 11.2, 15.13_

- [x] 19. Implement Bot_Interface: channel, update source, and voice baseline
  - [x] 19.1 Implement the MessagingChannel abstraction and Telegram adapter
    - Define the `MessagingChannel` interface (`send_text`, `send_payment_instructions`, `request_contact`, `download_file`) and a Telegram adapter using python-telegram-bot; inline-keyboard rendering with the elderly-friendly preset-button UX; no domain service imports the Telegram SDK
    - Render every user-facing string (prompts, confirmations, errors, notifications, button labels) by mapping the domain services' stable status/error codes and data placeholders through the `Message_Catalog` (task 2.4) for the active language, defaulting to Hindi with graceful fallback; emit brand strings language-independently
    - _Requirements: 1.1, 1.4, 1.5, 5.5, 6.1, 18.1, 18.2_
  - [x] 19.2 Implement the UpdateSource abstraction with authenticity verification
    - Define `UpdateSource` with `LongPollSource` (v1) and `WebhookSource` (Phase 2); `verify_authenticity` validates the `X-Telegram-Bot-Api-Secret-Token` header in webhook mode; updates failing verification are discarded with no state change
    - _Requirements: 12.3, 12.4, 13.8_
  - [x] 19.3 Implement the voice-message baseline handler
    - On a voice message reply within 5s confirming receipt, state voice is not interpreted in v1, and re-present the current step's button/text options; unprocessable-voice fallback retains the current step
    - _Requirements: 14.1, 14.2, 14.3, 14.4_
  - [ ]* 19.4 Write property test for update authenticity gating
    - **Property 33: Update authenticity gating (webhook mode)**
    - **Validates: Requirements 12.3, 12.4**
  - [ ]* 19.5 Write property test for voice acknowledgement and step preservation
    - **Property 31: Voice messages are always acknowledged and preserve the current step**
    - **Validates: Requirements 14.1, 14.2, 14.4**
  - [x] 19.6 Implement modern Telegram presentation features in the Telegram adapter
    - In the Telegram adapter (presentation layer only), register a persistent bot command menu via `setMyCommands` (customer commands such as browse, cart, my orders, help; the `/language` language-switch command; plus Seller-only commands gated by `Auth_Service.require_admin`), configure the chat Menu Button for one-tap access to the primary entry point, use callback-driven inline keyboards as the primary navigation mechanism, and offer large-label reply keyboards as an elderly-friendly option
    - Implement the **language-switch control available to both Customers and the Seller**: a `/language` command (registered in `setMyCommands`) and/or an inline button (offered on welcome/help screens and reachable on demand) that presents Hindi (हिंदी) and English as large, clearly labeled buttons; on selection it calls `Auth_Service.set_language_preference` (task 6.1) to persist the user's choice and **immediately re-renders subsequent content via the `Message_Catalog` in the selected language**, continuing in that language until the user switches again; the control is role-independent and never blocks any flow
    - Implement branded onboarding: the `/start` welcome introduces "Jan Purna (जन पूर्णा)" (using the centralized branding/strings module from 2.3) before presenting the Share Contact action
    - Keep all of these strictly as presentation-layer concerns inside the Telegram `MessagingChannel` adapter; domain services (Auth, Catalog, Cart, Order, Payment, Notification, Admin) stay channel-agnostic and never reference command menus, menu buttons, or keyboard types (per the design's "Modern Telegram Presentation" subsection)
    - _Requirements: 1.1, 1.4, 1.5, 18.3, 18.4, 18.8_

- [x] 20. Implement Admin_Console security gating and Seller screens
  - [x] 20.1 Implement Seller-only entry points with require_admin and fulfillment highlighting
    - Every Admin_Console entry point calls `Auth_Service.require_admin` first (reject non-Sellers with "Seller privileges required", no data change); wire catalog management, pending verifications, approvals, fulfillment transitions, modification, offline-flag toggle; render pending-verification and active-order lists with fulfillment-conflict flags + shortfalls that never block approval and offer modify/reject options
    - _Requirements: 1.7, 7.1, 10.6, 12.1, 15.3, 16.3, 16.4, 16.5, 16.6, 17.2_
  - [x] 20.2 Implement Seller settings: pickup location and UPI details
    - Implement `set_pickup_location` (1–500 chars), `set_upi_address` (VPA validation: non-empty local part + single `@` + non-empty handle), and `set_upi_qr` (supported image ≤10MB, store only the object-storage reference) — each calls `Auth_Service.require_admin` first and, on any validation/authorization failure, leaves the corresponding current value unchanged and responds stating the requirement; persist into the single-row `seller_settings` (migration 3.5)
    - Ensure `Payment_Service.present_instructions` (task 11.1) and the Ready-For-Pickup notification read the **current** seller-configured `upi_address` / `upi_qr_object_key` / `pickup_location`; implement the non-blocking behavior where marking an Order ready (APPROVED → READY_FOR_PICKUP) with no Pickup_Location configured **warns** the Seller but still performs the transition
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.9_
  - [ ]* 20.3 Write property test for Seller settings round-trip, authorization, and validation
    - **Property 34: Seller settings round-trip, authorization, and validation**
    - **Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8**

- [x] 21. Checkpoint - services and interface complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 22. Wiring and integration
  - [x] 22.1 Implement the update router (one transaction per inbound update)
    - Build the `ConversationHandler`/callback router that opens one DB transaction per inbound update, verifies authenticity, routes to the correct service, renders channel-agnostic results back as text + inline keyboard, and rolls back atomically on any error
    - _Requirements: 1.9, 12.3, 12.4, 12.6_
  - [x] 22.2 Implement the application entrypoint
    - Wire config, repositories, all services, the notification retry worker, and `LongPollSource` into a single runnable process; expose a config switch for webhook mode with no data-format change
    - _Requirements: 13.1, 13.2, 13.8_
  - [ ]* 22.3 Write end-to-end integration tests
    - Telegram round-trip (contact sharing, button callbacks, file download, voice receipt) against a staging bot; object-storage upload/download + signed-URL retrieval; notification retry timing against a fake failing transport (≥30s spacing, ≤3 retries); two-worker webhook smoke against one DB with no schema change
    - _Requirements: 6.3, 11.3, 13.8, 14.1_

- [x] 23. Backup, restore, and durability verification (code/CI configurable)
  - [x] 23.1 Implement automated backup configuration and scheduling
    - Add backup configuration/scripts creating catalog/order/payment backups at ≤24h intervals with ≥30-day retention; alert the Seller on backup failure while preserving existing data
    - _Requirements: 13.4, 13.6_
  - [x] 23.2 Implement the restore-drill script with timing assertion
    - Add a restore script/drill that restores from a retained backup and asserts completion under 60 minutes, alerting the Seller on restore failure while preserving stored data
    - _Requirements: 13.5, 13.6_
  - [ ]* 23.3 Write durability/retention and no-SMS smoke checks
    - Verify ≥365-day durable retention surviving single-node failure (PITR/replication config check) and assert messaging is Telegram-only with no SMS provider wired
    - _Requirements: 13.1, 13.2, 13.3_

- [x] 24. Final checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test/verification sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each Correctness Property P1a–P35 is implemented by exactly one property-based test (Hypothesis, `max_examples >= 100`), tagged `Feature: cottonseed-oilcake-marketplace, Property N: ...`.
- P15 (no-oversell) uses the design's model-based testing approach: a reference stock model compared against the implementation after randomized concurrent operation sequences on a transactional test DB.
- Property tests are placed close to the implementation they validate to catch errors early; example/integration tests cover the EXAMPLE/EDGE_CASE criteria and infrastructure round-trips.
- Each task references the specific requirements (and, for test tasks, the property) it implements for full traceability.
- Checkpoints (tasks 5, 9, 15, 21, 24) provide incremental validation points.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.2", "2.3", "2.4"] },
    { "id": 2, "tasks": ["3.1", "2.5", "2.6"] },
    { "id": 3, "tasks": ["3.2"] },
    { "id": 4, "tasks": ["3.3", "3.5"] },
    { "id": 5, "tasks": ["3.4", "4.1"] },
    { "id": 6, "tasks": ["4.2", "4.4"] },
    { "id": 7, "tasks": ["4.3", "6.1", "7.1", "7.2", "10.1", "13.1", "18.1", "19.1", "19.6"] },
    { "id": 8, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "7.3", "7.4", "7.5", "7.6", "8.1", "10.6", "13.2", "18.2", "19.2"] },
    { "id": 9, "tasks": ["7.7", "7.8", "7.9", "8.2", "8.3", "10.2", "18.4", "19.3", "19.4"] },
    { "id": 10, "tasks": ["10.3", "10.4", "10.5", "11.1", "11.2", "12.1", "12.2", "14.1", "14.2", "19.5"] },
    { "id": 11, "tasks": ["11.3", "11.4", "12.3", "12.4", "12.5", "14.3", "14.4", "14.5", "16.1"] },
    { "id": 12, "tasks": ["16.2", "17.1", "18.3", "18.5", "20.1"] },
    { "id": 13, "tasks": ["16.3", "16.4", "16.5", "16.6", "17.2", "17.3", "17.4", "20.2", "22.1", "23.1"] },
    { "id": 14, "tasks": ["20.3", "22.2", "23.2"] },
    { "id": 15, "tasks": ["22.3", "23.3"] }
  ]
}
```

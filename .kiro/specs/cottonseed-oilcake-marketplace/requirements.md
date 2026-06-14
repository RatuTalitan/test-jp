# Requirements Document

## Introduction

This document specifies the requirements for a single-vendor ordering marketplace for cotton seed oil cake (cattle feed), delivered as a **Telegram bot** for version 1 (v1). The marketplace serves one Seller (the business owner, who is also the Admin) and many Customers, who are predominantly elderly users in India.

The Telegram bot platform was selected to eliminate per-message SMS/OTP authentication cost (Telegram provides a verified phone number via its built-in Share Contact action), to support native voice messages for elderly users, and to provide free push notifications. Payment is handled manually via UPI (QR code plus UPI address) with manual verification by the Seller; there is no payment gateway. There is no delivery logistics — Customers collect orders from a fixed pickup location. The architecture must minimize initial development and hosting cost while remaining scalable, and must include appropriate security controls.

This document captures functional and non-functional requirements only (the "what"), leaving implementation choices to the design phase.

### Assumptions

Assumptions A1, A2, and A3 have been **confirmed by the Seller** during review, with the clarifications recorded below. Assumptions A4 through A7 remain applied defaults and SHOULD be confirmed or corrected by the Seller.

- **A1 — Units & pricing (CONFIRMED):** The Seller chooses the Unit (one of kilogram, quintal, or bag) per Product at listing time. All three units are supported. Each Product carries a price per Unit and a Seller-defined Minimum_Order_Quantity. Stock quantity is tracked per Product.
- **A2 — Stock decrement timing (CONFIRMED):** Stock quantity is decremented when the Seller approves (accepts) the Order — that is, on the transition to the Approved state (see Requirement 7). Stock is not reserved or decremented at placement or at payment submission.
- **A3 — Payment proof (CONFIRMED):** Payment proof is the UPI transaction reference (UTR); a payment screenshot image is optional. A new offline-payment exception applies for Seller-flagged trusted Customers, who may place and have Orders approved without submitting a UTR (see Requirement 17).
- **A4 — Notifications:** Status-change notifications are delivered as Telegram messages to both Customer and Seller; no SMS is used.
- **A5 — Voice/AI assistant:** Deferred to Phase 2 (documented in this document as optional/out-of-scope for v1 acceptance), except basic voice-message receipt which v1 acknowledges.
- **A6 — Scale:** One Seller; tens of Customers initially, with the architecture able to grow to thousands.
- **A7 — Languages and default:** Both Hindi and English are fully supported customer-facing Bot_Interface languages, each with complete message-catalog coverage. Hindi is the default language for a new or unknown user (one with no stored preference), because the audience is predominantly elderly rural Hindi speakers. Users can switch between Hindi and English at any time, and the System remembers each user's choice as a persisted per-user Language_Preference (see Requirement 18).

## Glossary

- **System**: The complete Telegram-bot-based marketplace application, including all subsystems below.
- **Bot_Interface**: The subsystem that sends and receives Telegram messages, commands, buttons, images, and voice messages to and from users.
- **Auth_Service**: The subsystem that establishes and stores a user's verified identity using the Telegram-provided contact (phone number) and Telegram user identifier.
- **Catalog_Service**: The subsystem that stores and serves products, categories, units, prices, descriptions, and stock quantities.
- **Cart_Service**: The subsystem that holds a Customer's selected products and quantities prior to order placement.
- **Order_Service**: The subsystem that creates orders, stores order line items, and manages order state transitions.
- **Payment_Service**: The subsystem that presents UPI payment details (QR code and UPI address) and records Customer-submitted payment proof.
- **Notification_Service**: The subsystem that delivers status and event messages to Customers and the Seller.
- **Admin_Console**: The set of Seller-only bot commands and screens for catalog management, order review, payment verification, and order-state transitions.
- **Seller**: The single business owner, who also holds the Admin role.
- **Customer**: A buyer who browses products and places orders.
- **Admin**: The privileged role held by the Seller, authorized to manage the catalog and orders.
- **Product**: A single sellable item of cotton seed oil cake with a category, unit, price, description, and stock quantity.
- **Category**: A grouping label assigned to Products (for example, a grade or variety of cotton seed oil cake).
- **Unit**: The measurement basis for a Product's price and quantity, one of: kilogram, quintal, bag.
- **Minimum_Order_Quantity**: The smallest quantity, expressed in a Product's Unit, that a Customer may order for that Product.
- **Cart**: The collection of pending product selections belonging to one Customer.
- **Order**: A confirmed Customer request containing one or more line items, an order state, and payment information.
- **Order_State**: The current lifecycle status of an Order, one of: Placed, Payment Pending, Payment Submitted, Payment Verified, Approved, Ready For Pickup, Completed, Cancelled, Rejected.
- **UPI**: Unified Payments Interface, the Indian instant payment system used for manual payment.
- **UPI_Address**: The payee Virtual Payment Address (VPA) shown to Customers in payment instructions, configurable by the Seller through the Admin_Console (see Requirement 19).
- **UPI_QR_Code**: The QR code image shown to Customers in payment instructions, configurable by the Seller through the Admin_Console and stored as a reference to an uploaded image (see Requirement 19).
- **Message_Catalog**: The centralized store of all user-facing strings used by the Bot_Interface — including prompts, confirmations, errors, notifications, and button labels — keyed for retrieval by language, so that copy is never hard-coded and the presentation language can be extended or changed without altering domain logic. The Message_Catalog SHALL contain complete entries for both Hindi and English, providing full coverage of every user-facing string in each language (see Requirement 18).
- **Language_Preference**: A per-user stored choice of presentation language, one of Hindi or English, persisted by the System for an identified user. When a user has no stored Language_Preference (a new or unknown user), the System treats the user's effective language as Hindi by default (see Requirement 18).
- **UTR**: Unique Transaction Reference, the identifier generated by a UPI transaction, submitted by a Customer as payment proof. The accepted UTR format is a configured pattern (the **default** is exactly 12 alphanumeric characters) so that the Seller or operator can adjust it to match their UPI app; see Requirement 6.
- **Monetary_Rounding**: The rule governing how monetary amounts are expressed and rounded. All monetary amounts are expressed in Indian rupees to two decimal places (paise). Whenever a monetary amount is computed (for example a line total or an order total), the value is rounded to two decimal places using the half-up rule, meaning a digit of 5 or greater at the third decimal place rounds the second decimal place up and a digit below 5 rounds it down.
- **Pickup_Location**: The Seller-defined physical location where Customers collect completed orders, expressed as a text address or description and configurable by the Seller through the Admin_Console (see Requirement 19).
- **Verified_Contact**: A phone number obtained through Telegram's Share Contact action and associated with a Telegram user identifier.
- **Offline_Payment_Allowed**: A Seller-controlled per-Customer flag indicating that the identified Customer is permitted to place Orders and have them approved without submitting in-app payment proof (a UTR), because payment is settled through a medium outside the System.
- **Audit_Trail**: An append-only record of significant Seller-initiated actions on an Order — including order modifications and offline-payment approvals — where each entry records what changed, the old value, the new value, the acting user, and a timestamp.
- **Fulfillable**: A property of an Order with respect to current Product stock; an Order is fully Fulfillable when, for every line item, the line item's ordered quantity is less than or equal to the corresponding Product's current stock quantity. A line item's shortfall is its ordered quantity minus the Product's current stock quantity when that difference is greater than zero.
- **Pre_Fulfillment_State**: Any Order_State from which an Order has not yet been prepared for pickup, namely one of: Placed, Payment Pending, Payment Submitted, Payment Verified, or Approved.

## Requirements

### Requirement 1: Identity and Authentication via Telegram Contact

**User Story:** As an elderly Customer without email, I want to start using the marketplace by sharing my Telegram contact, so that I can be identified without typing passwords or one-time codes.

#### Acceptance Criteria

1. WHEN a user starts the Bot_Interface for the first time, THE Bot_Interface SHALL request the user's contact using Telegram's Share Contact action within 5 seconds.
2. WHEN a user shares a contact through Telegram and the shared contact's Telegram user identifier matches the sender's Telegram user identifier, THE Auth_Service SHALL store the Verified_Contact associated with the user's Telegram user identifier.
3. WHEN a returning user, defined as a user whose Telegram user identifier already has a stored Verified_Contact, sends a message, THE Auth_Service SHALL identify the user by the Telegram user identifier without requesting the contact again.
4. IF a user declines to share a contact or sends a message without having shared a contact, THEN THE Bot_Interface SHALL respond with a message explaining that a shared contact is required to place orders and SHALL re-present the Share Contact action.
5. THE Bot_Interface SHALL be permitted to send the contact-sharing explanation and present the Share Contact action proactively at any time, including before the user's first interaction.
6. THE Auth_Service SHALL assign the Admin role only to the Telegram user identifier configured as the Seller and SHALL assign the Customer role to all other Telegram user identifiers.
7. IF a user without the Admin role sends an Admin_Console command, THEN THE Admin_Console SHALL not execute the command and SHALL respond that the action requires Seller privileges.
8. IF a user shares a contact whose Telegram user identifier does not match the sender's Telegram user identifier, THEN THE Auth_Service SHALL reject the contact, store no Verified_Contact, and re-present the Share Contact action.
9. IF storing the Verified_Contact fails, THEN THE Auth_Service SHALL store no partial identity, retain the user as unauthenticated, and respond that identification could not be completed.

### Requirement 2: Seller Catalog and Category Management

**User Story:** As the Seller, I want to manage my product catalog and categories, so that Customers see accurate products, descriptions, and rates.

#### Acceptance Criteria

1. WHEN the Seller submits a new Product with a name of 1 to 100 characters, a Category, a Unit, a price per Unit, a description of up to 1,000 characters, a Minimum_Order_Quantity, and a stock quantity, and all submitted values are valid, THE Catalog_Service SHALL create the Product and respond with a confirmation message identifying the created Product by name.
2. THE Catalog_Service SHALL allow a Product to be created and listed with a stock quantity of zero.
3. WHEN the Seller updates a Product's price, description, Category, Unit, Minimum_Order_Quantity, or stock quantity with values that satisfy the validation rules in criteria 5, 6, and 9, THE Catalog_Service SHALL persist the updated values and respond with a confirmation message identifying the updated Product.
4. WHEN the Seller marks a Product as unavailable, THE Catalog_Service SHALL exclude that Product from Customer-facing browsing results.
5. IF the Seller submits a Product with a price per Unit less than zero or greater than 9,999,999.99, THEN THE Catalog_Service SHALL reject the submission, create no Product record, and respond with a message identifying the price per Unit as invalid and stating the accepted range.
6. IF the Seller submits a Product with a Minimum_Order_Quantity less than or equal to zero or greater than 9,999,999, THEN THE Catalog_Service SHALL reject the submission, create no Product record, and respond with a message identifying the Minimum_Order_Quantity as invalid and stating the accepted range.
7. THE Catalog_Service SHALL store each Product's Unit as one of: kilogram, quintal, bag.
8. WHEN the Seller creates a Category with a name of 1 to 50 characters that does not match an existing Category name, THE Catalog_Service SHALL create the Category and make it available for assignment to Products.
9. IF the Seller submits a Product with a stock quantity less than zero or greater than 9,999,999, THEN THE Catalog_Service SHALL reject the submission, create no Product record, and respond with a message identifying the stock quantity as invalid and stating the accepted range.
10. IF the Seller submits a Product missing a name, Category, Unit, price per Unit, or Minimum_Order_Quantity, THEN THE Catalog_Service SHALL reject the submission, create no Product record, and respond with a message identifying each missing required field.
11. IF the Seller creates a Category with a name that matches an existing Category name, THEN THE Catalog_Service SHALL reject the creation and respond with a message indicating that the Category name already exists.

### Requirement 3: Customer Product Browsing

**User Story:** As a Customer, I want to browse available products by category and view their rates and details, so that I can decide what to order.

#### Acceptance Criteria

1. WHEN a Customer requests the product list, THE Catalog_Service SHALL return all available Products grouped by Category, where an available Product is a Product the Seller has not marked unavailable.
2. WHEN a Customer selects a Product, THE Bot_Interface SHALL display the Product's name, Category, description, Unit, price per Unit, Minimum_Order_Quantity, and an availability indicator that reads "in stock" when the Product's stock quantity is greater than zero and "out of stock" when the Product's stock quantity is exactly zero.
3. WHILE a Product's stock quantity is exactly zero, THE Catalog_Service SHALL display the Product as out of stock and SHALL prevent that Product from being added to a Cart, regardless of the Product's Minimum_Order_Quantity.
4. WHEN a Customer requests products in a specific Category, THE Catalog_Service SHALL return only the available Products in that Category.
5. IF a Customer requests the product list when no available Products exist, THEN THE Catalog_Service SHALL respond with a message indicating that no products are currently available.
6. IF a Customer requests products in a Category that does not exist or that contains no available Products, THEN THE Catalog_Service SHALL respond with a message indicating that no products are available in that Category.

### Requirement 4: Cart Management

**User Story:** As a Customer, I want to add products and quantities to a cart, so that I can order multiple items together.

#### Acceptance Criteria

1. WHEN a Customer adds a Product with a quantity that is greater than zero, at least the Product's Minimum_Order_Quantity, and not greater than the Product's current stock quantity, THE Cart_Service SHALL store the Product and quantity as a line item in that Customer's Cart and SHALL confirm the addition.
2. IF a Customer adds a quantity below the Product's Minimum_Order_Quantity, THEN THE Cart_Service SHALL reject the addition and respond with the required Minimum_Order_Quantity.
3. IF a Customer adds a quantity greater than the Product's current stock quantity, THEN THE Cart_Service SHALL reject the addition and respond with the available stock quantity.
4. IF a Customer adds a quantity less than or equal to zero, THEN THE Cart_Service SHALL reject the addition and respond that the quantity must be greater than zero.
5. IF a Customer adds a Product that already exists as a line item in the Customer's Cart, THEN THE Cart_Service SHALL combine the added quantity with the existing line item's quantity and SHALL re-validate the combined quantity against the Product's Minimum_Order_Quantity and current stock quantity.
6. WHEN a Customer changes the quantity of a Cart line item to a new quantity that is greater than zero, at least the Product's Minimum_Order_Quantity, and not greater than the Product's current stock quantity, THE Cart_Service SHALL update that line item to the new quantity.
7. IF a Customer changes a Cart line item to a quantity that is less than or equal to zero, below the Product's Minimum_Order_Quantity, or greater than the Product's current stock quantity, THEN THE Cart_Service SHALL reject the change, retain the line item's existing quantity, and respond with the applicable Minimum_Order_Quantity or available stock quantity.
8. WHEN a Customer removes a line item from the Cart, THE Cart_Service SHALL delete that line item from the Cart.
9. WHEN a Customer views a Cart that contains at least one line item, THE Cart_Service SHALL display each line item's Product name, quantity, and Unit, its line total computed as the line item quantity multiplied by the Product's price per Unit and then rounded to two decimal places using the half-up rule defined by Monetary_Rounding, and the Cart total amount computed as the sum of all rounded line totals.

### Requirement 5: Order Placement

**User Story:** As a Customer, I want to place an order from my cart, so that the Seller can prepare my cattle feed for pickup.

#### Acceptance Criteria

1. WHEN a Customer confirms placement of a non-empty Cart, THE Order_Service SHALL create an Order assigned a unique Order identifier, containing all line items copied from the Cart, and SHALL set the Order_State to Placed.
2. WHEN an Order is created, THE Order_Service SHALL record the unique Order identifier, the ordering Customer's identifier, each line item with its Product and ordered quantity, the per-line amounts each computed as the ordered quantity multiplied by the Product's price per Unit and then rounded to two decimal places using the half-up rule defined by Monetary_Rounding, the total amount computed as the sum of the rounded per-line amounts, and the creation timestamp.
3. IF a Customer attempts to place an Order from an empty Cart, THEN THE Order_Service SHALL reject the request, leave the Cart unchanged, and respond that the Cart is empty.
4. IF, at placement time, one or more line item quantities exceed their Product's current stock quantity, THEN THE Order_Service SHALL reject placement, leave the Cart unchanged, and respond identifying every affected line item together with each affected Product's available stock quantity.
5. WHEN an Order is created, THE Order_Service SHALL transition the Order_State from Placed to Payment Pending and SHALL present the Customer with payment instructions consisting of the Seller's UPI address, a UPI QR code, and the total amount due.
6. WHEN an Order is successfully created from a Cart, THE Cart_Service SHALL remove all line items from that Customer's Cart.
7. WHEN an Order transitions to Payment Pending, THE Notification_Service SHALL notify the Seller that a new Order awaits payment.

### Requirement 6: Manual UPI Payment and Proof Submission

**User Story:** As a Customer, I want to pay through UPI and submit my payment reference, so that the Seller can confirm my payment without a payment gateway.

#### Acceptance Criteria

1. WHILE an Order is in the Payment Pending state, THE Payment_Service SHALL present to the Customer the Seller-configured UPI_Address and UPI_QR_Code (set through the Admin_Console as defined in Requirement 19) together with the total amount due, defined as the Order's total amount.
2. WHILE an Order is in the Payment Pending state, WHEN a Customer submits a UTR that matches the configured UTR format (default: exactly 12 alphanumeric characters) for that Order, THE Payment_Service SHALL record the UTR against the Order and SHALL transition the Order_State to Payment Submitted.
3. WHERE a Customer attaches a payment screenshot image in a supported image format not exceeding 10 MB, THE Payment_Service SHALL store the image with the Order.
4. IF a Customer submits a UTR that is already recorded against a different Order, THEN THE Payment_Service SHALL reject the submission, leave the Order_State unchanged, and respond that the reference is already in use.
5. WHEN an Order transitions to Payment Submitted, THE Notification_Service SHALL notify the Seller that payment proof is ready for verification.
6. IF a Customer submits a UTR that does not match the configured UTR format (default: exactly 12 alphanumeric characters), THEN THE Payment_Service SHALL reject the submission, leave the Order_State unchanged, and respond stating the required UTR format.
7. IF a Customer submits a UTR WHILE the Order is not in the Payment Pending state, THEN THE Payment_Service SHALL reject the submission and leave the Order_State unchanged.
8. IF a Customer attaches a payment screenshot image in an unsupported format or exceeding 10 MB, THEN THE Payment_Service SHALL reject the attachment and respond stating the accepted image format and size.

### Requirement 7: Seller Payment Verification and Order Approval

**User Story:** As the Seller, I want to review payment proof and approve or reject orders, so that I only fulfill orders that have been paid.

#### Acceptance Criteria

1. WHEN the Seller requests pending verifications, THE Admin_Console SHALL list all Orders in the Payment Submitted state with their UTR, total amount due, and any attached payment screenshot image.
2. WHILE an Order is in the Payment Submitted state, WHEN the Seller marks the Order's payment as verified, THE Order_Service SHALL transition the Order_State from Payment Submitted to Payment Verified.
3. WHILE every line item's ordered quantity is less than or equal to the corresponding Product's current stock quantity, WHEN an Order transitions to Payment Verified, THE Order_Service SHALL transition the Order_State to Approved and SHALL reduce each line item's Product stock quantity by the ordered quantity.
4. IF, when an Order transitions to Payment Verified, any line item's ordered quantity exceeds the corresponding Product's current stock quantity, THEN THE Order_Service SHALL leave the Order in the Payment Verified state, SHALL leave all Product stock quantities unchanged, and SHALL notify the Seller identifying each affected line item and its available stock quantity.
5. WHILE an Order is in the Payment Submitted state, IF the Seller rejects the Order's payment with a reason of 1 to 500 characters, THEN THE Order_Service SHALL transition the Order_State to Rejected and SHALL record the Seller-provided reason.
6. IF the Seller submits a payment rejection with no reason or with a reason exceeding 500 characters, THEN THE Order_Service SHALL reject the action, SHALL leave the Order_State unchanged, and SHALL respond requesting a reason of 1 to 500 characters.
7. IF the Seller attempts to mark an Order's payment as verified or to reject an Order's payment WHILE the Order is not in the Payment Submitted state, THEN THE Order_Service SHALL reject the action, SHALL leave the Order_State unchanged, and SHALL respond that the Order is not awaiting payment verification.
8. WHEN an Order transitions to Approved, THE Notification_Service SHALL notify the Customer that the Order is approved.
9. WHEN an Order transitions to Rejected, THE Notification_Service SHALL notify the Customer of the rejection and the recorded reason.

### Requirement 8: Order Fulfillment and Pickup

**User Story:** As the Seller, I want to mark orders ready for pickup and completed, so that Customers know when and where to collect their feed.

#### Acceptance Criteria

1. WHEN the Seller marks an Approved Order as ready, THE Order_Service SHALL transition the Order_State to Ready For Pickup.
2. WHEN an Order transitions to Ready For Pickup, THE Notification_Service SHALL notify the Customer of the Seller-configured Pickup_Location (set through the Admin_Console as defined in Requirement 19) and each line item's Product name and ordered quantity.
3. WHEN the Seller marks a Ready For Pickup Order as collected, THE Order_Service SHALL transition the Order_State to Completed.
4. IF the Seller attempts to mark an Order as collected WHILE the Order is not in the Ready For Pickup state, THEN THE Order_Service SHALL reject the action, leave the Order_State unchanged, and respond that the Order must be Ready For Pickup first.
5. IF the Seller attempts to mark an Order as ready WHILE the Order is not in the Approved state, THEN THE Order_Service SHALL reject the action, leave the Order_State unchanged, and respond that the Order must be Approved first.
6. WHEN an Order transitions to Completed, THE Notification_Service SHALL notify the Customer that the Order is complete.
7. IF any Order_State transition is attempted on a Completed Order, THEN THE Order_Service SHALL reject the transition and leave the Order_State unchanged.
8. THE Order_Service SHALL permit Order_State transitions only along the sequence Placed → Payment Pending → Payment Submitted → Payment Verified → Approved → Ready For Pickup → Completed, except for the Cancelled and Rejected transitions defined in this document and the offline-payment approval path defined in Requirement 17.
9. WHERE the ordering Customer has Offline_Payment_Allowed enabled, THE Order_Service SHALL additionally permit the Order to transition from Payment Pending directly to Payment Verified without a recorded UTR and without passing through Payment Submitted, after which the standard Payment Verified → Approved transition of Requirement 7 applies; no other Order_State sequence changes are introduced.

### Requirement 9: Order Cancellation

**User Story:** As a Customer, I want to cancel an order before it is approved, so that I am not committed to a purchase I no longer want.

#### Acceptance Criteria

1. WHILE an Order is in the Placed, Payment Pending, or Payment Submitted state, THE Order_Service SHALL allow the Customer who placed the Order to initiate cancellation of that Order.
2. WHEN the Customer who placed the Order confirms cancellation of an Order that is in the Placed, Payment Pending, or Payment Submitted state, THE Order_Service SHALL transition the Order_State to Cancelled within 5 seconds.
3. WHEN an Order transitions to Cancelled, THE Order_Service SHALL send the cancelling Customer a confirmation message indicating that the Order has been cancelled.
4. IF a Customer attempts to cancel an Order in the Approved, Ready For Pickup, Completed, or Cancelled state, THEN THE Order_Service SHALL reject the cancellation, leave the Order_State unchanged, and respond with a message indicating that the Order can no longer be cancelled.
5. IF a user who is not the Customer who placed the Order attempts to cancel that Order, THEN THE Order_Service SHALL reject the cancellation, leave the Order_State unchanged, and respond with a message indicating that the user is not authorized to cancel the Order.
6. WHEN an Order transitions to Cancelled, THE Notification_Service SHALL notify the Seller of the cancellation within 10 seconds.

### Requirement 10: Order Status Tracking

**User Story:** As a Customer, I want to check the current status of my orders, so that I know what to expect and when to pick up.

#### Acceptance Criteria

1. WHEN a Customer requests order history, THE Order_Service SHALL return that Customer's Orders, each with its current Order_State and creation timestamp, ordered by creation timestamp from most recent to oldest.
2. IF a Customer requests order history and that Customer has no Orders, THEN THE Order_Service SHALL respond with a message indicating that no Orders exist.
3. WHEN a Customer selects one of that Customer's own Orders, THE Order_Service SHALL display the Order's line items, total amount, current Order_State, and recorded payment reference.
4. WHERE the selected Order has no recorded payment reference, THE Order_Service SHALL display the payment reference field as not yet provided in place of a reference value.
5. IF a Customer selects an Order that does not belong to that Customer, THEN THE Order_Service SHALL reject the request and respond with a message indicating the Order is not available to that Customer, without disclosing the Order's details.
6. WHEN the Seller requests active orders, THE Admin_Console SHALL return all Orders that are not in the Completed, Cancelled, or Rejected state, ordered by creation timestamp from oldest to most recent.

### Requirement 11: Notifications

**User Story:** As a Customer and as the Seller, I want to receive a message whenever an order changes status, so that I stay informed without checking manually.

#### Acceptance Criteria

1. WHEN an Order_State transition occurs, THE Notification_Service SHALL send a Telegram message to the affected Customer within 10 seconds of the transition, and the message SHALL identify the Order and state the new Order_State.
2. WHEN a new Order reaches the Payment Pending state, THE Notification_Service SHALL send a Telegram message to the Seller within 10 seconds of the transition, and the message SHALL identify the Order.
3. IF delivery of a Telegram notification fails, THEN THE Notification_Service SHALL record the delivery failure together with the Order identifier and the intended recipient, and SHALL retry delivery up to 3 additional times with at least 30 seconds between consecutive attempts.
4. IF all retry attempts for a Telegram notification fail, THEN THE Notification_Service SHALL mark the notification as undelivered and SHALL leave the affected Order's Order_State unchanged.
5. WHEN the Notification_Service successfully delivers a notification after one or more failed attempts, THE Notification_Service SHALL mark the recorded delivery failure as resolved.

### Requirement 12: Security and Data Protection

**User Story:** As the Seller, I want the marketplace to protect access and personal data, so that customer information and order integrity are preserved.

#### Acceptance Criteria

1. IF a user not configured as the Seller invokes an Admin_Console function, THEN THE System SHALL reject the invocation, leave all data unchanged, and respond that Seller privileges are required.
2. THE System SHALL store each Verified_Contact and payment proof such that the data is readable and writable only by the Auth_Service, Order_Service, Payment_Service, and the Seller, and SHALL expose no Customer's Verified_Contact or payment proof to any Customer other than the data owner.
3. WHEN the System receives an incoming update, THE Bot_Interface SHALL verify that the update originates from the configured Telegram bot before performing any state change or data write.
4. IF an incoming update fails bot-origin verification, THEN THE Bot_Interface SHALL discard the update without processing it and SHALL make no state change.
5. THE System SHALL store credentials and secret tokens outside of any source code and configuration files committed to version control.
6. IF an unauthenticated user, defined as a user without a Verified_Contact, attempts to place an Order, THEN THE Order_Service SHALL reject the request, create no Order, and re-present the Share Contact action.

### Requirement 13: Cost, Hosting, and Scalability

**User Story:** As the Seller, I want the marketplace to run at minimal cost and scale when needed, so that the business is affordable now and can grow later.

#### Acceptance Criteria

1. THE System SHALL exchange all messaging through the Telegram Bot API at zero per-message charge.
2. THE System SHALL authenticate Customers using the Telegram Verified_Contact at zero SMS charge and zero one-time-code charge.
3. THE System SHALL retain catalog, order, and payment data in durable storage for at least 365 days such that the data survives the failure of a single storage node.
4. THE System SHALL create an automated backup of catalog, order, and payment data at intervals of no more than 24 hours and SHALL retain each backup for at least 30 days.
5. WHEN the Seller initiates a restore from a retained backup, THE System SHALL complete the restore within 60 minutes.
6. IF a backup or restore operation fails, THEN THE System SHALL preserve the existing stored data and SHALL notify the Seller of the failure.
7. THE System SHALL define stable, versioned data formats for catalog, order, and payment data from initial release, independent of whether horizontal scaling is implemented.
8. WHERE the number of concurrent active Customers exceeds 100, THE System SHALL support adding horizontal message-processing capacity without changes to the stored data formats.

### Requirement 14: Voice Message Handling (v1 Baseline)

**User Story:** As an elderly Customer, I want the bot to acknowledge a voice message I send, so that I am not left without a response when I speak instead of type.

#### Acceptance Criteria

1. WHEN a Customer sends a Telegram voice message, THE Bot_Interface SHALL reply within 5 seconds with a Telegram text message confirming the voice message was received.
2. WHEN the Bot_Interface confirms receipt of a Customer's voice message, THE Bot_Interface SHALL present, in the same reply, the button and text options available for the Customer's current step.
3. WHERE the voice assistant feature is disabled, THE Bot_Interface SHALL present a Telegram message stating that voice input is not processed in v1 and SHALL display the button and text options the Customer can use to continue.
4. IF the Bot_Interface receives a voice message it cannot process, THEN THE Bot_Interface SHALL respond within 5 seconds with a message indicating the voice message could not be handled and SHALL re-present the available button and text options, retaining the Customer's current step.

### Requirement 15: Seller Order Modification

**User Story:** As the Seller, I want to correct or modify an Order's contents after it has been placed, so that I can fix mistakes such as a wrong item or quantity that the Customer and I agreed to change.

#### Acceptance Criteria

1. WHILE an Order is in a Pre_Fulfillment_State (one of Placed, Payment Pending, Payment Submitted, Payment Verified, or Approved), THE Order_Service SHALL allow the Seller to modify the Order by adding a line item, removing a line item, or changing an existing line item's quantity.
2. IF the Seller attempts to modify an Order WHILE the Order is in the Ready For Pickup, Completed, Cancelled, or Rejected state, THEN THE Order_Service SHALL reject the modification, leave the Order unchanged, and respond that the Order can no longer be modified in its current state.
3. IF a user who is not the Seller attempts to modify any Order, THEN THE Order_Service SHALL reject the modification, leave the Order unchanged, and respond that the action requires Seller privileges.
4. IF the Seller attempts to modify an Order whose Order identifier does not match an existing Order, THEN THE Order_Service SHALL reject the modification, make no data change, and respond that the Order was not found.
5. WHEN the Seller adds a line item or changes a line item's quantity, THE Order_Service SHALL require the resulting line item quantity to be greater than zero and at least the Product's Minimum_Order_Quantity, and IF the resulting quantity is less than or equal to zero or below the Product's Minimum_Order_Quantity, THEN THE Order_Service SHALL reject the modification, leave the Order unchanged, and respond with the Product's Minimum_Order_Quantity.
6. WHILE the Order is in the Placed, Payment Pending, Payment Submitted, or Payment Verified state, IF the Seller adds a line item or increases a line item's quantity such that the resulting ordered quantity exceeds the Product's current stock quantity, THEN THE Order_Service SHALL reject the modification, leave the Order and all Product stock quantities unchanged, and respond identifying the affected Product and its available stock quantity.
7. WHERE the Order is in the Approved state, in which this Order's quantities have already been decremented from Product stock, WHEN the Seller changes a line item's quantity, THE Order_Service SHALL adjust the affected Product's stock quantity by the difference between the previous ordered quantity and the new ordered quantity, returning stock to the Product when the quantity is reduced and deducting additional stock from the Product when the quantity is increased.
8. WHERE the Order is in the Approved state, IF the Seller increases a line item's quantity or adds a line item by an amount that exceeds the affected Product's current stock quantity available for the increase, THEN THE Order_Service SHALL reject the modification, leave the Order and all Product stock quantities unchanged, and respond identifying the affected Product and its available stock quantity.
9. WHERE the Order is in the Approved state, WHEN the Seller removes a line item or reduces a line item's quantity, THE Order_Service SHALL return the corresponding decremented quantity to the affected Product's stock quantity.
10. IF a modification would reduce the Order to zero line items, THEN THE Order_Service SHALL reject the modification, leave the Order unchanged, and respond that an Order must retain at least one line item and that the Seller should cancel or reject the Order instead.
11. WHEN the Seller completes a valid modification of an Order, THE Order_Service SHALL recalculate each line item's per-line amount as the line item's ordered quantity multiplied by that line item's snapshot unit price and then rounded to two decimal places using the half-up rule defined by Monetary_Rounding, and SHALL recalculate the Order's total amount as the sum of all rounded per-line amounts.
12. WHEN the Seller completes a valid modification of an Order, THE System SHALL append an Audit_Trail entry recording the change applied, the affected line item or Product, the old value, the new value, the acting Seller, and the timestamp of the modification.
13. WHEN the Seller completes a valid modification of an Order, THE Notification_Service SHALL notify the Customer who placed the Order of the change and of the recalculated total amount within 10 seconds.
14. WHEN the Seller completes a valid modification of an Order, THE Order_Service SHALL leave the Order_State unchanged.
15. WHEN the Seller adds a new line item to an Order during a modification, THE Order_Service SHALL snapshot onto the new line item the Product's current catalog price per Unit at the time of modification and SHALL use that snapshot unit price as the new line item's unit price for all subsequent recalculation.
16. THE Order_Service SHALL retain each existing line item's original snapshot unit price unless that line item's quantity is changed during the modification, and the recalculation defined in criterion 11 SHALL use each line item's snapshot unit price.

### Requirement 16: Fulfillment Conflict Detection and Highlighting

**User Story:** As the Seller, I want Orders that can no longer be fully fulfilled from current stock to be highlighted, so that I do not unknowingly approve Orders that oversell my stock when multiple pending Orders compete for the same Product.

#### Acceptance Criteria

1. WHEN a Product's stock quantity changes, whether due to an approval-time decrement, a stock return from an Order modification or cancellation, or a Seller stock edit, THE System SHALL recompute, for every Order that is not yet Approved (that is, every Order in the Placed, Payment Pending, Payment Submitted, or Payment Verified state) and that contains that Product, whether each such Order is fully Fulfillable from current stock.
2. THE System SHALL determine that an Order is fully Fulfillable WHEN every line item's ordered quantity is less than or equal to the corresponding Product's current stock quantity, and SHALL determine that an Order is not fully Fulfillable WHEN at least one line item's ordered quantity exceeds the corresponding Product's current stock quantity.
3. WHEN the Seller views pending verifications or active orders, THE Admin_Console SHALL flag each listed Order that is not fully Fulfillable, and for each such Order SHALL identify every affected line item together with its shortfall, expressed as the line item's ordered quantity and the affected Product's current stock quantity.
4. WHERE an Order is fully Fulfillable, THE Admin_Console SHALL present the Order without a fulfillment-conflict flag.
5. THE Admin_Console SHALL NOT block the Seller from approving a flagged Order on the basis of the flag alone; the approval-time stock check in Requirement 7 criterion 4 SHALL govern the actual stock decrement and SHALL prevent overselling.
6. WHERE an Order is flagged as not fully Fulfillable, THE Admin_Console SHALL present the Seller the option to modify the Order as defined in Requirement 15 or to reject the Order.

### Requirement 17: Offline Payment Bypass for Trusted Customers

**User Story:** As the Seller, I want to allow specific trusted Customers to place and have Orders approved without submitting in-app payment proof, so that Customers who pay me through another medium are not blocked by the UTR requirement.

#### Acceptance Criteria

1. WHEN the Seller enables or disables the Offline_Payment_Allowed flag for a Customer identified by Verified_Contact or Telegram user identifier, THE Admin_Console SHALL persist the flag's new value for that Customer and respond confirming the Customer and the flag's new value.
2. IF a user who is not the Seller attempts to enable or disable the Offline_Payment_Allowed flag for any Customer, THEN THE Admin_Console SHALL reject the action, leave the flag unchanged, and respond that the action requires Seller privileges.
3. IF the Seller attempts to enable or disable the Offline_Payment_Allowed flag for a Customer identifier that does not match any known Customer, THEN THE Admin_Console SHALL reject the action, persist no flag, and respond that the Customer was not found.
4. WHERE a Customer has the Offline_Payment_Allowed flag enabled, WHEN that Customer confirms placement of a non-empty Cart, THE Order_Service SHALL create the Order and transition it to Payment Pending without requiring a UTR, leaving the Order in a state from which the Seller can subsequently approve it, and SHALL NOT auto-approve the Order.
5. WHERE the ordering Customer has the Offline_Payment_Allowed flag enabled, WHEN the Seller approves the Order, THE Order_Service SHALL allow the Order to transition through Payment Verified to Approved without a recorded UTR and SHALL apply the stock-decrement and Fulfillability checks defined in Requirement 7 criteria 3 and 4.
6. WHERE the ordering Customer has the Offline_Payment_Allowed flag enabled, THE Payment_Service SHALL still accept an optional UTR submitted by that Customer for the Order, recording it against the Order as defined in Requirement 6.
7. WHERE the ordering Customer does not have the Offline_Payment_Allowed flag enabled, THE Order_Service SHALL require the standard UTR payment flow defined in Requirements 5, 6, and 7, and SHALL NOT permit approval of the Order without a recorded UTR.
8. WHEN an Order is approved while the ordering Customer's Offline_Payment_Allowed flag is enabled and no UTR is recorded against the Order, THE System SHALL append an Audit_Trail entry recording that the Order was approved under the offline-payment exception, the acting Seller, and the timestamp of the approval.

### Requirement 18: Language and Localization

**User Story:** As an elderly rural Hindi-speaking Customer, I want the bot to work fully in Hindi by default while letting me switch to English and remember my choice, so that I can use the marketplace in whichever of the two languages I understand best.

#### Acceptance Criteria

1. THE System SHALL support two presentation languages, Hindi and English, with full Message_Catalog coverage for both, such that every prompt, confirmation, error, notification, and button label is available in both Hindi and English.
2. WHILE an identified user has no stored Language_Preference, THE Bot_Interface SHALL present all customer-facing content in Hindi as the default language.
3. THE Bot_Interface SHALL provide a language-switch control, presented as a button or command, that allows a user to switch the presentation language between Hindi and English at any time.
4. WHEN a user selects a presentation language using the language-switch control, THE System SHALL persist that user's Language_Preference as the selected language and SHALL present subsequent customer-facing content in the selected language until the user changes the Language_Preference again.
5. WHILE an identified user has a stored Language_Preference, THE Bot_Interface SHALL present all customer-facing content — including prompts, confirmations, errors, notifications, and button labels — in the language of that stored Language_Preference.
6. THE Bot_Interface SHALL retrieve every user-facing string from the Message_Catalog and SHALL NOT use any hard-coded user-facing copy, and THE Message_Catalog SHALL contain complete Hindi and English entries for every user-facing string.
7. THE Bot_Interface SHALL present the brand strings "Jan Purna", "जन पूर्णा", and "JP" consistently regardless of the active presentation language.
8. THE System SHALL make the language-switch control and persisted Language_Preference available to both Customers and the Seller.
9. THE System SHALL NOT block, abort, or suspend any flow because of language, and IF a requested user-facing string is missing from the Message_Catalog for the active language, THEN THE Bot_Interface SHALL present the string from the other supported language, preferring Hindi, and SHALL continue the current flow without interruption.

### Requirement 19: Seller-Managed Pickup Location and UPI Payment Details

**User Story:** As the Seller, I want to set and update my pickup location and UPI payment details, so that Customers always receive my current collection address and the correct payment instructions.

#### Acceptance Criteria

1. WHEN the Seller submits a Pickup_Location consisting of 1 to 500 characters, THE Admin_Console SHALL persist the Pickup_Location as the current value and respond confirming the updated Pickup_Location.
2. IF the Seller submits a Pickup_Location that is empty or exceeds 500 characters, THEN THE Admin_Console SHALL reject the submission, leave the current Pickup_Location unchanged, and respond stating that the Pickup_Location must be 1 to 500 characters.
3. WHEN the Seller submits a UPI_Address that satisfies the Virtual Payment Address format, defined as a non-empty local part, followed by a single "@" character, followed by a non-empty handle, THE Admin_Console SHALL persist the UPI_Address as the current value and respond confirming the updated UPI_Address.
4. IF the Seller submits a UPI_Address that does not satisfy the Virtual Payment Address format, THEN THE Admin_Console SHALL reject the submission, leave the current UPI_Address unchanged, and respond stating the required UPI_Address format.
5. WHEN the Seller uploads a UPI_QR_Code image in a supported image format not exceeding 10 MB, THE Admin_Console SHALL store the image as a reference, set it as the current UPI_QR_Code, and respond confirming the updated UPI_QR_Code.
6. IF the Seller uploads a UPI_QR_Code image in an unsupported format or exceeding 10 MB, THEN THE Admin_Console SHALL reject the upload, leave the current UPI_QR_Code unchanged, and respond stating the accepted image format and size.
7. THE Payment_Service SHALL present to Customers, as the payment instructions defined in Requirements 5 and 6, the Seller-configured UPI_Address and UPI_QR_Code current values set through this Requirement.
8. IF a user who is not the Seller attempts to set or update the Pickup_Location, the UPI_Address, or the UPI_QR_Code, THEN THE Admin_Console SHALL reject the action, leave the current values unchanged, and respond that the action requires Seller privileges.
9. WHILE no Pickup_Location has been configured, THE System SHALL continue to operate and SHALL NOT block any flow, and WHEN the Seller marks an Order as ready while no Pickup_Location has been configured, THE Admin_Console SHALL warn the Seller that a Pickup_Location must be set, without hard-blocking the action.

## Out of Scope for v1 (Phase 2 Candidates)

The following are documented for future planning and are NOT part of v1 acceptance:

- **Voice/AI assistant interpretation:** Transcribing and understanding voice queries such as "what is in stock today?" (Customer) or "show today's orders" (Seller), including speech-to-text and intent handling.
- **WhatsApp channel:** Migrating or extending the bot to the WhatsApp Business platform after Telegram validation.
- **Automated payment gateway integration:** Replacing manual UPI verification with an automated gateway.
- **Delivery/logistics:** Any shipping, courier, or address-based delivery (v1 is pickup-only).
- **Multi-seller marketplace:** Supporting more than one Seller.

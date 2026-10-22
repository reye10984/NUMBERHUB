# NumberHub

NumberHub uses 5SIM for the real number catalogue, activation purchases, and SMS status checks. SMS messages appear in an order only when 5SIM returns them.

## Configuration

- `SECRET_KEY`: a long, private application secret.
- `DATABASE_PATH`: SQLite database path.
- `FIVE_SIM_API_KEY`: API key for 5SIM.
- `PAYSTACK_SECRET_KEY`: reserved for a future Paystack wallet integration; it is not implemented yet.

Never commit or share real credentials.

## Important purchase behavior

The current catalogue purchase action submits a real purchase request to 5SIM and can spend the configured 5SIM account balance. Do not submit it unless you intend to purchase a number.

Paystack deposits, customer-wallet-funded purchases, and automatic wallet refunds are not implemented yet. The wallet ledger does not currently fund orders.

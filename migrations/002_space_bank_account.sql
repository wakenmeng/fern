-- Each demo space gets its own Column sandbox bank account (DESIGN §2.1). Null with the simulated bank.
ALTER TABLE spaces ADD COLUMN bank_account_id text UNIQUE;
ALTER TABLE spaces ADD COLUMN bank_account_number_id text;

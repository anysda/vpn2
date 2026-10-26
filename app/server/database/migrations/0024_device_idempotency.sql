CREATE TABLE `device_requests` (
	`idempotency_key` text PRIMARY KEY NOT NULL,
	`request_hash` text NOT NULL,
	`client_id` integer NOT NULL,
	`device_id` integer NOT NULL,
	`created_at` integer DEFAULT (strftime('%s','now')) NOT NULL
);

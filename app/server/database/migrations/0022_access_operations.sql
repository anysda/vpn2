CREATE TABLE `access_operations` (
	`id` text PRIMARY KEY NOT NULL,
	`client_id` integer NOT NULL,
	`device_id` integer,
	`kind` text NOT NULL,
	`version` integer NOT NULL,
	`idempotency_key` text NOT NULL,
	`request_hash` text NOT NULL,
	`completion_recorded` integer DEFAULT false NOT NULL,
	`status` text NOT NULL,
	`protocols` text NOT NULL,
	`payload` text NOT NULL,
	`actor` text NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL,
	`next_retry` integer NOT NULL,
	`created_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `access_operations_idempotency_key_unique` ON `access_operations` (`idempotency_key`);--> statement-breakpoint
CREATE UNIQUE INDEX `access_operations_version` ON `access_operations` (`client_id`,`version`);--> statement-breakpoint
CREATE INDEX `access_operations_due` ON `access_operations` (`status`,`next_retry`);--> statement-breakpoint
ALTER TABLE `clients` ADD `access_version` integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE `clients` ADD `deleting` integer DEFAULT false NOT NULL;--> statement-breakpoint
ALTER TABLE `devices` ADD `deleting` integer DEFAULT false NOT NULL;

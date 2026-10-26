ALTER TABLE `devices` ADD `idempotency_key` text;--> statement-breakpoint
ALTER TABLE `devices` ADD `request_hash` text;--> statement-breakpoint
CREATE UNIQUE INDEX `devices_idempotency_key_unique` ON `devices` (`idempotency_key`);
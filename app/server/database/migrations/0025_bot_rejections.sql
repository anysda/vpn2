ALTER TABLE `bot_outbox` ADD `recipients` integer DEFAULT 1 NOT NULL;--> statement-breakpoint
ALTER TABLE `bot_outbox` ADD `rejected` integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE `bot_outbox` ADD `rejected_code` integer;
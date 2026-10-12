CREATE TABLE `bot_outbox` (
	`id` text PRIMARY KEY NOT NULL,
	`label` text NOT NULL,
	`payload` text NOT NULL,
	`status` text NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL,
	`last_failure` integer DEFAULT 0 NOT NULL,
	`due` integer NOT NULL,
	`expires` integer NOT NULL,
	`created` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `bot_outbox_due_idx` ON `bot_outbox` (`status`,`due`);

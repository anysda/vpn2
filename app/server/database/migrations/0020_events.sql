CREATE TABLE `events` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`at` integer NOT NULL,
	`type` text NOT NULL,
	`level` text NOT NULL,
	`object` text,
	`actor` text,
	`title` text NOT NULL,
	`detail` text DEFAULT '' NOT NULL,
	`key` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `events_key_unique` ON `events` (`key`);--> statement-breakpoint
CREATE INDEX `events_at_idx` ON `events` (`at`);--> statement-breakpoint
CREATE INDEX `events_object_idx` ON `events` (`object`,`at`);
DROP TABLE `clients`;--> statement-breakpoint
CREATE TABLE `clients` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`name` text NOT NULL,
	`filter_traffic` integer DEFAULT true NOT NULL,
	`expires_at` integer,
	`device_limit` integer DEFAULT 3,
	`frozen_manual` integer DEFAULT false NOT NULL,
	`password` text NOT NULL,
	`created_at` integer DEFAULT (strftime('%s','now')) NOT NULL,
	`updated_at` integer DEFAULT (strftime('%s','now')) NOT NULL
);
--> statement-breakpoint
CREATE TABLE `devices` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`client_id` integer NOT NULL,
	`name` text NOT NULL,
	`wg_private_key` text,
	`wg_public_key` text,
	`wg_preshared_key` text,
	`wg_ip` text,
	`ovpn_cert` text,
	`ovpn_key` text,
	`rx_total` integer DEFAULT 0 NOT NULL,
	`tx_total` integer DEFAULT 0 NOT NULL,
	`created_at` integer DEFAULT (strftime('%s','now')) NOT NULL,
	`updated_at` integer DEFAULT (strftime('%s','now')) NOT NULL,
	FOREIGN KEY (`client_id`) REFERENCES `clients`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `devices_client_idx` ON `devices` (`client_id`);

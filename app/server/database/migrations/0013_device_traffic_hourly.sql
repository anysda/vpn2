CREATE TABLE `device_traffic_hourly` (
	`device_id` integer NOT NULL,
	`hour` integer NOT NULL,
	`rx` integer DEFAULT 0 NOT NULL,
	`tx` integer DEFAULT 0 NOT NULL,
	PRIMARY KEY(`device_id`, `hour`),
	FOREIGN KEY (`device_id`) REFERENCES `devices`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `device_traffic_hourly_hour_idx` ON `device_traffic_hourly` (`hour`);
CREATE TABLE `admin_config_password` (
	`user_id` integer PRIMARY KEY NOT NULL,
	`hash` text NOT NULL,
	FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON UPDATE no action ON DELETE cascade
);

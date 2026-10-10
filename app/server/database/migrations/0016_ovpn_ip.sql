ALTER TABLE `devices` ADD `ovpn_ip` text;--> statement-breakpoint
CREATE UNIQUE INDEX `devices_ovpn_ip_unique` ON `devices` (`ovpn_ip`);
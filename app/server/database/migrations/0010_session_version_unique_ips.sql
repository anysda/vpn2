ALTER TABLE `users` ADD `session_version` integer DEFAULT 0 NOT NULL;--> statement-breakpoint
-- Дубли адресов уже могли накопиться из-за гонки выдачи (VPN2-50), а с ними
-- UNIQUE не создастся. Адрес остаётся у самого раннего устройства, у
-- остальных обнуляется: ключи целы, при следующей выдаче конфига
-- ensureDeviceWg/ensureDeviceIkev2 займут им свободный адрес. Их туннель и так
-- не работал — на дублирующемся адресе ответы уходят только одному пиру.
UPDATE `devices` SET `wg_ip` = NULL WHERE `wg_ip` IS NOT NULL AND `id` NOT IN (SELECT MIN(`id`) FROM `devices` WHERE `wg_ip` IS NOT NULL GROUP BY `wg_ip`);--> statement-breakpoint
UPDATE `devices` SET `ikev2_ip` = NULL WHERE `ikev2_ip` IS NOT NULL AND `id` NOT IN (SELECT MIN(`id`) FROM `devices` WHERE `ikev2_ip` IS NOT NULL GROUP BY `ikev2_ip`);--> statement-breakpoint
CREATE UNIQUE INDEX `devices_wg_ip_unique` ON `devices` (`wg_ip`);--> statement-breakpoint
CREATE UNIQUE INDEX `devices_ikev2_ip_unique` ON `devices` (`ikev2_ip`);

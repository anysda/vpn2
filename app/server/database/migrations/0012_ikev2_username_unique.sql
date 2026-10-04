-- Дубли логина IKEv2 уже могли накопиться из-за гонки выдачи (VPN2-70), а с
-- ними UNIQUE не создастся. Логин остаётся у самого раннего устройства, у
-- остальных снимаются логин и пароль (адрес остаётся): syncIkev2 выдаст им
-- новый свободный логин. Со старым они и так не входили — swanctl на одном id
-- принимает только один пароль.
UPDATE `devices` SET `ikev2_username` = NULL, `ikev2_password` = NULL WHERE `ikev2_username` IS NOT NULL AND `id` NOT IN (SELECT MIN(`id`) FROM `devices` WHERE `ikev2_username` IS NOT NULL GROUP BY `ikev2_username`);--> statement-breakpoint
CREATE UNIQUE INDEX `devices_ikev2_username_unique` ON `devices` (`ikev2_username`);

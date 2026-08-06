# SSO в панель через Authentik (OIDC)

Панель умеет отдавать вход внешнему OIDC-провайдеру. Проверено на
[Authentik](https://goauthentik.io/); других IdP не пробовали, но обработчик
общий — нужен только discovery-документ.

Выключено по умолчанию. Без блока `sso:` в `config.yaml` панель ведёт себя
ровно как раньше: логин и пароль, TOTP, rate-limit.

---

## Что получается в итоге

- Неавторизованного человека сразу уносит в Authentik — формы он не видит вообще.
- Обратно возвращается уже с сессией панели.
- Пароль **не удаляется**, а прячется: форма живёт по адресу
  `https://<домен>/login?direct=1`. Это break-glass на случай, когда Authentik
  недоступен, — и он не зависит от Authentik.
- Панель **никогда** не заводит учётку из IdP. Вход привязывается к уже
  существующей учётке и только по явному списку.

## Модель доступа (почему так строго)

В этом флоте каждый пользователь Authentik — суперюзер. Если бы панель заводила
учётку по факту успешного входа, любой человек из IdP становился бы её админом.
Поэтому:

1. **Связка по `sub`** — uuid пользователя в IdP (`sub_mode: user_uuid` у
   провайдера). Не по имени: имя человек меняет сам. Не по почте: почта в панели
   и в IdP расходится, а auto-link по почте — вектор захвата (завёл в IdP юзера
   с чужой почтой → вошёл под чужим).
2. **Явный allow-список** `sso.allowed_subs`. Пустой = не войдёт никто; стадия
   деплоя падает заранее, чтобы это не выяснилось на живом входе.
3. **Первый вход только привязывает**, не создаёт. Нет локальной учётки —
   отказ (`sso_no_local_user`), а не «сейчас заведём».

## Порядок подключения

Порядок именно такой: сначала провайдер в IdP, потом uuid, и только потом
включение входа. Наоборот нельзя — окно между «включили» и «привязали» это
ровно то место, где рождаются дубли учёток.

### 1. Завести OIDC-провайдер и приложение в Authentik

Нужны:

| поле | значение |
|---|---|
| client type | confidential |
| client id | `vpn2` |
| redirect uri | `https://<домен панели>/auth/authentik`, matching mode **strict** |
| grant types | `authorization_code`, `refresh_token` |
| sub mode | `user_uuid` |
| scopes | `openid`, `profile`, `email` |

⚠️ У провайдера, созданного **не** через веб-форму (blueprint, API), поле
`grant_types` приезжает пустым, и вход падает с `invalid_request / The request
is otherwise malformed`. Задавать явно.

### 2. Узнать `sub` пользователя

`sub` при `sub_mode: user_uuid` — это поле `uuid` пользователя в Authentik
(не `pk`, не имя). В API:

```bash
curl -s -H "Authorization: Bearer <ak-token>" \
  'https://<authentik>/api/v3/core/users/?username=<логин>' \
  | jq -r '.results[0].uuid'
```

### 3. Заполнить `config.yaml` на entry-ноде

```yaml
sso:
  enabled: true
  base_url: https://auth.example.com    # адрес IdP
  app_slug: vpn2                        # slug приложения в Authentik
  client_id: vpn2
  client_secret: "<секрет OIDC-клиента>"
  allowed_subs: "<uuid>"                # или "<uuid>:<логин в панели>", через запятую
  auto_redirect: true                   # true — формы не видно вообще
  password_login: true                  # пароль спрятан, но ЖИВ
  label: Authentik
```

discovery-документ собирается как
`{base_url}/application/o/{app_slug}/.well-known/openid-configuration`. Если IdP
не Authentik — задать `discovery_url:` целиком.

`redirect_uri` панель строит сама из `panel.domain`, править её негде и не надо
— но она обязана совпасть с тем, что записано у провайдера.

### 4. Выкатить

```bash
./deploy.sh 30-frontend ru
```

Стадия трогает только контейнер панели и Caddy. WireGuard, OpenVPN, IKEv2,
sing-box и mesh до экзитов не затрагиваются — **туннели клиентов не рвутся**.

---

## Требования и ограничения

- **Только HTTPS** (`panel.domain` обязателен). Куки `state`/PKCE/`nonce`
  ставятся с флагом `Secure`; по HTTP они не сохранятся и каждый вход будет
  падать в «state mismatch». Стадия 30 падает заранее, если SSO включён без
  домена.
- **Панель должна дотягиваться до IdP по сети** — discovery и обмен кода на
  токен идут с самой entry-ноды, а не из браузера.
- Локальный TOTP при входе через SSO не спрашивается: второй фактор — забота
  IdP. На парольном break-glass-входе TOTP работает как работал.
- Выйти из панели, пока жива сессия в самом Authentik, «насовсем» нельзя:
  кнопка «Выйти» кладёт человека на `/login?direct=1`, откуда автоматического
  возврата в IdP уже нет.

## Break-glass

| ситуация | что делать |
|---|---|
| Authentik лежит / SSO сломан | `https://<домен>/login?direct=1` → логин и пароль |
| пароль забыт | `cat /etc/anysda/admin-password.txt` на entry-ноде |
| надо выключить SSO совсем | `sso.enabled: false` в `config.yaml` → `./deploy.sh 30-frontend ru` |
| срочно, без деплоя | `docker rm -f anysda-vpn2` и запустить контейнер без `NUXT_PUBLIC_SSO_ENABLED` (проще прогнать стадию) |

⚠️ `password_login: false` рубит парольный вход наглухо — **вместе с
break-glass**. Ставить осознанно и только когда есть второй путь внутрь.

## Коды ошибок на форме

| код | что значит |
|---|---|
| `sso_off` | рубильник выключен или клиент недонастроен |
| `sso_failed` | обмен с IdP не удался: отказ, несовпадение state/nonce, ошибка токена |
| `sso_not_allowed` | вход прошёл, но `sub` не в `allowed_subs` |
| `sso_no_local_user` | в панели нет учётки, к которой привязывать |
| `sso_ambiguous` | учёток в панели несколько — нужен формат `<sub>:<логин>` |
| `sso_already_linked` | учётка панели уже привязана к другому `sub` |

Подробности каждого отказа — в логе панели (`docker logs anysda-vpn2`), наружу
отдаётся только код.

## Как это устроено в коде

| файл | что делает |
|---|---|
| `app/server/routes/auth/authentik.get.ts` | точка входа и callback `/auth/authentik` |
| `app/server/utils/sso.ts` | allow-список, привязка по `sub`, отказы |
| `app/app/middleware/auth.global.ts` | авторедирект неавторизованного в IdP |
| `app/app/pages/login.vue` | форма break-glass, кнопка входа, коды ошибок |
| `infra/scripts/30-frontend.sh` | env контейнера + защёлка «SSO без HTTPS» |
| `infra/lib/config2env.py` | блок `sso:` из `config.yaml` → `ru.env` |

Взят **общий** обработчик OIDC из `nuxt-auth-utils`
(`defineOAuthOidcEventHandler`), а не готовый `defineOAuthAuthentikEventHandler`
из той же библиотеки: у второго нет ни `state`, ни PKCE, ни `nonce` — то есть
нет защиты от login-CSRF. Цена — один запрос за discovery-документом на вход.

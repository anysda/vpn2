<script setup lang="ts">
useHead({ title: 'Настройки — anysda-vpn2' })
definePageMeta({ title: 'Настройки', sub: 'Бот, профиль и вход в панель' })
</script>

<template>
  <main class="tab on stg">
    <div class="head">
      <div>
        <h1>Настройки</h1>
        <p>Бот, профиль и вход в панель</p>
      </div>
    </div>
    <section class="sgrid">
      <SettingsProfile />
      <BotsSection />
      <SettingsSso />
    </section>
  </main>
</template>

<style>
/* Настройки: классы макета #t-settings, под корнем .stg */
.stg .sgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.stg .card h3{font-size:14px;font-weight:500;color:var(--t2)}
.stg .sc{padding:20px 22px;display:flex;flex-direction:column;min-height:270px}
.stg .sh{display:flex;justify-content:space-between;align-items:center}
.stg .sh h3{color:var(--t1);font-size:15px}
.stg .ha{display:flex;gap:8px;align-items:center}
.stg .tag{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:999px;background:rgba(var(--fg),.06);color:var(--t2);font-size:11.5px;white-space:nowrap}
.stg .tag.ok{color:var(--green);border:1px solid rgba(var(--grn-rgb),.3);background:rgba(var(--grn-rgb),.06)}
.stg .tag.badt{color:var(--red);border:1px solid rgba(var(--red-rgb),.35);background:rgba(var(--red-rgb),.08)}
.stg .sbig{margin:18px 0 14px}
.stg .sbig .big{font-size:34px}
.stg .ys{display:grid;grid-template-columns:repeat(3,1fr);margin:0 0 6px}
.stg .ys div{display:flex;flex-direction:column;gap:4px;padding-left:14px;border-left:1px solid rgba(var(--fg),.07)}
.stg .ys div:first-child{padding-left:0;border-left:0}
.stg .ys span{font-size:11.5px;color:var(--t3)}
.stg .ys b{font-weight:400;font-size:17px}
.stg .fld{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid rgba(var(--fg),.05);margin-top:auto}
.stg .fld+.fld{margin-top:0}
.stg .sh+.fld{margin-top:14px}
.stg .fld>span:first-child{color:var(--t3);font-size:12.5px;width:170px;flex:none}
.stg .fld code{font:400 13px var(--font-sans);color:var(--t1);flex:1}
.stg .fld .pill{height:30px;font-size:12px}
.stg .fld .in{flex:1;height:36px}
.stg .fld .hl{flex:none;max-width:210px;font-size:11.5px;color:var(--t3);line-height:1.35}
.stg .fld .xb{flex:none}
.stg .sub2{color:var(--t3);font-size:12.5px;margin:0}
.stg .fld code.sub2{color:var(--t3);font-size:12.5px}
.stg .al b{color:var(--t1);font-weight:500}
.stg .al.red b{color:var(--red-t)}
.stg .sc .al+.fld,.stg .sc .al+.pwf>.fld:first-child{margin-top:10px}
.stg .pwf{display:contents}
.stg fieldset.fg2{border:0;margin:0;padding:0;min-width:0}
.stg .ssso{grid-column:2;grid-row:1/3}
.stg .tog{width:42px;height:24px;border-radius:999px;border:0;padding:0;background:var(--tog);position:relative;cursor:pointer;flex:none;transition:background .2s}
.stg .tog i{position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:var(--tog-k);transition:transform .25s var(--ease),background-color .2s}
.stg .tog.on{background:var(--acc);box-shadow:0 0 14px rgba(var(--glow),calc(.3*var(--gk)))}
.stg .tog.on i{transform:translateX(18px);background:var(--acc-t)}
.stg .tog:active i{transform:scaleX(1.25);transform-origin:left}
.stg .tog.on:active i{transform:translateX(18px) scaleX(1.25);transform-origin:right}
.stg .tog:disabled{cursor:default}
.stg .xb,.stg-modal .xb,.stg-sheet .xb{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;border:1px solid var(--line);background:none;color:var(--t3);cursor:pointer;transition:transform .16s var(--ease),color .2s,background-color .2s}
@media (hover:hover){.stg .xb:hover,.stg-modal .xb:hover{color:var(--t1)}}
.stg .xb:active,.stg-modal .xb:active,.stg-sheet .xb:active{transform:scale(.9)}
.stg .xb svg,.stg-modal .xb svg,.stg-sheet .xb svg{width:14px;height:14px}
.stg .pill.red{color:var(--red-t);border-color:rgba(var(--red-rgb),.3)}
.stg .mt,.stg-modal .mt{color:var(--t2);font-size:13px;line-height:1.55;margin-top:8px}
.stg #tfa{margin-top:22px}
.stg #tfa .sh{padding-top:16px;border-top:1px solid rgba(var(--fg),.08)}
.stg #tfa .mt{margin-top:14px}
.stg #tfa form{display:contents}
.stg .qrrow{display:flex;gap:18px;align-items:flex-end}
.stg .qrrow .in{margin-bottom:4px}
.stg #tfa .qr{flex:none;display:block;max-width:100%;margin:14px 0 0;padding:12px;border-radius:18px;background:#fff;box-shadow:0 0 40px rgba(var(--glow),calc(.12*var(--gk)))}
.stg #tfa .qr svg{display:block;max-width:100%;height:auto}
.stg #tfa .fld.qrcode{margin-top:14px}
/* модалка «Выключить 2FA?» - .ovl/.modal макета поверх UModal */
.stg-ovl{background:var(--ovl);backdrop-filter:blur(6px)}
.stg-modal.card{width:min(460px,calc(100vw - 48px));max-height:calc(100vh - 48px);overflow:auto;padding:22px 24px;background:linear-gradient(180deg,var(--pop-a),var(--pop-b));scrollbar-width:thin;scrollbar-color:rgba(var(--fg),.14) transparent}
.stg-modal[data-state=open]{animation:stg-m-in .26s var(--ease)}
.stg-modal[data-state=open] form>*{animation:stg-m-row .3s var(--ease) backwards}
.stg-modal[data-state=open] form>:nth-child(2){animation-delay:75ms}
.stg-modal[data-state=open] form>:nth-child(3){animation-delay:110ms}
.stg-modal[data-state=open] form>:nth-child(4){animation-delay:145ms}
.stg-modal form>:first-child{animation-delay:40ms}
.stg-modal .mh,.stg-sheet .mh{display:flex;align-items:center;justify-content:space-between;gap:12px}
.stg-modal .mh h2,.stg-sheet .mh h2{font-size:18px;font-weight:500;letter-spacing:-.01em}
.stg-modal .pill.redw,.stg-sheet .pill.redw{background:var(--red);border-color:var(--red);color:#fff}
@keyframes stg-m-in{from{transform:translateY(8px) scale(.97)}}
@keyframes stg-m-row{from{opacity:0;transform:translateY(6px)}}
/* узкий ПК: в две колонки подписи и поля не вмещаются — карточки одна под другой */
@media (min-width:721px) and (max-width:1000px){
  .stg .sgrid{grid-template-columns:minmax(0,1fr)}
  .stg .ssso{grid-column:auto;grid-row:auto}
}
/* телефон: одна колонка, подпись над значением, ряды и кнопки как в мобильном макете */
@media (max-width:720px){
  .stg .sgrid{grid-template-columns:minmax(0,1fr)}
  .stg .ssso{grid-column:auto;grid-row:auto}
  .stg .sc{padding:20px 18px;min-height:0}
  .stg .sh{gap:8px}
  .stg .sc>.al{margin-top:4px}
  .stg .sc div.fld:not(:has(>button))>span:first-child{width:120px}
  /* поле: подпись сверху, под полем подсказка; кнопка «показать» у поля справа */
  .stg .sc label.fld,.stg .sc .al+.pwf>label.fld:first-child{flex-wrap:wrap;gap:7px 8px;padding:0;margin-top:14px;border-top:0}
  .stg .sc label.fld>span:first-child{width:auto;flex:1 0 100%;font-size:12px}
  .stg .sc label.fld .in{height:40px}
  .stg .sc label.fld .hl{flex:1 0 100%;max-width:none}
  /* ряд из одних кнопок — справа */
  .stg .sc div.fld:has(>span:empty),.stg .sc div.fld:has(>code:empty){justify-content:flex-end;gap:8px;padding:0;margin-top:14px;border-top:0}
  .stg .sc div.fld:has(>code:empty)>span,.stg .sc div.fld>span:empty,.stg .sc div.fld>code:empty{display:none}
  .stg #tfa div.fld:has(>code:empty){margin-top:12px}
  /* тумблеры и проверка: подпись и пояснение слева, кнопка справа */
  .stg .ssso div.fld:not(:has(>code:empty)){display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:12px;align-items:center;padding:12px 0;margin-top:14px;border-top:1px solid rgba(var(--fg),.05)}
  .stg .ssso div.fld:not(:has(>code:empty))+div.fld{margin-top:0}
  .stg .ssso div.fld:not(:has(>code:empty))>span:first-child{grid-area:1/1;width:auto;color:var(--t1);font-size:13px}
  .stg .ssso div.fld:not(:has(>code:empty))>code{grid-area:2/1;color:var(--t3);font-size:11.5px;line-height:1.45}
  .stg .ssso div.fld:not(:has(>code:empty))>button{grid-area:1/2/3/3}
  .stg .ssso div.fld:has(>code.sub2){margin-top:12px}
  .stg .ssso div.fld:has(>code.sub2)>span:first-child{color:var(--t3);font-size:12.5px}
  .stg .ssso div.fld:has(>code.sub2)>code{font-size:12px}
  .stg .sc div.fld:has(>code.sub2)+label.fld{margin-top:4px}
  .stg .ssso fieldset>div.fld:last-child{padding-top:12px;border-top:1px solid rgba(var(--fg),.05)}
  /* бот: длинный ник и подписи счётчиков не раздвигают карточку */
  .stg .sbig .big{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .stg .ys{grid-template-columns:repeat(3,minmax(0,1fr))}
  .stg .ys span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* настройка 2FA: QR над секретом */
  .stg .qrrow{flex-direction:column;align-items:stretch;gap:12px}
  .stg #tfa .qr{align-self:center}
  .stg .qrrow .in{margin-bottom:0;height:40px}
  .stg button{touch-action:manipulation}
  .stg .pill,.stg .tog,.stg .xb{-webkit-user-select:none;user-select:none}
  .stg-sheet .fl2 .in{height:44px}
  .stg-sheet button{touch-action:manipulation;-webkit-user-select:none;user-select:none}
}
@media (prefers-reduced-motion:reduce){.stg-modal[data-state=open],.stg-modal[data-state=open] form>*{animation:none}}
</style>

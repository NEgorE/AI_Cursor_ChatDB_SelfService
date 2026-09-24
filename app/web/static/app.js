/* ChatDB SelfService — минимальный клиентский JS: подтверждения,
   показ chat_id для групповых триггеров, автоскрытие флеш-сообщений. */

document.addEventListener("DOMContentLoaded", () => {
  // Подтверждение опасных действий
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // chat_id виден только для групповых триггеров
  const typeRow = document.getElementById("trigger-type-row");
  const chatField = document.getElementById("chat-id-field");
  if (typeRow && chatField) {
    const sync = () => {
      const group = typeRow.querySelector('input[value="group"]');
      chatField.hidden = !(group && group.checked);
    };
    typeRow.querySelectorAll("input[name='trigger_type']").forEach((radio) => {
      radio.addEventListener("change", sync);
    });
    sync();
  }

  // Флеш-сообщения исчезают сами
  document.querySelectorAll(".flash.ok, .flash.err").forEach((flash) => {
    setTimeout(() => {
      flash.style.transition = "opacity 0.4s ease";
      flash.style.opacity = "0";
      setTimeout(() => flash.remove(), 450);
    }, 6000);
  });
});

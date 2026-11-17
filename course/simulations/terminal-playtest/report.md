# One terminal teaching playtest — synthetic evidence

This is one isolated assistant role-play using the existing classroom, a real PTY, and a local fixed HTTP fixture. No model was loaded and no model quality was measured. No learner/teacher grading was performed by the coordinator.

Lesson: 03. Actual HTTP requests: 2. Teacher messages through Session.advise: 2. Learner-selected console commands relayed: 16. Exact chosen-command/relay match: True.

The fixed fixture returned identical bytes on both calls, as designed. Changing that output was not a completion requirement for the learning difficulty.

Initial-case analysis evidence (ask events 26 and 51): the learner named drink, stated expected tea versus received coffee, and gave the disposition ‘JSON разбирается — да; схеме соответствует — да; содержание верно — нет’, citing the tea rule. The learner did not separately use accept/reject wording. The exact utterances and executed commands are preserved; the independent judge decides whether this resolves the initial difficulty.

## Observed pretest

Пока я считаю, что успешная проверка JSON Schema доказывает правильность ответа и понимание заявки: если все поля допустимы, дополнительная проверка содержания кажется лишней. Посмотрю текущий вход, получу ответ синтетического стенда и проверю его. Это не оценка качества модели.

Стенд вернул coffee для заявки о двух чашках чая. Это меня смущает: я ожидал, что схема отклонит неверный напиток. Проверяющая программа завершилась ошибкой No module named 'jsonschema', поэтому успешной проверки пока нет. Моё исходное мнение остаётся таким: если ответ пройдёт схему, этого должно быть достаточно для признания его правильным.

## Terminal interaction

### Advice 1 (event 20)

Сначала выполни `read validator.json`: в журнале появилась исправленная проверка того же необработанного ответа, без нового HTTP-запроса. Найди `exit_code`, `stdout` и `scope` и объясни через `ask`, что проверка действительно проверила. Затем сам запиши ожидаемые `drink`, `cups`, `urgent` из исходной заявки и сопоставь с ответом. Отдельно вынеси три вердикта с основаниями: разбирается ли JSON, соответствует ли схеме, верно ли содержание. Для расходящегося поля покажи правило заявки и фактическое значение ответа. Сохрани исходный ответ без правок и пересмотри свой вывод о достаточности схемы, опираясь на эти свидетельства. Это разбор синтетической фикстуры, а не измерение качества модели.

Learner replies:

По исходной заявке я ожидаю drink="tea", cups=2, urgent=false. В полученном ответе отличается drink: там "coffee". Перечитаю исправленный результат проверки, чтобы понять, что она приняла, и сохраню исходный ответ без исправлений.

Я был неправ: схема действительно приняла ответ с неверным напитком. JSON разбирается, структура проходит проверку, но содержание не соответствует заявке. Проверка не сравнивала ответ с исходным текстом, поэтому нужна отдельная сверка значений по правилам задачи. Исходный ответ сохранён без исправлений.

Actual learner-selected commands:

```text
read validator.json
write original-response.txt
{"drink":"coffee","cups":2,"urgent":false}
::end
ask Исправленная проверка того же ответа дала exit_code=0; в stdout указаны valid=true, check="syntax_and_schema_only", errors=[]; scope="syntax_and_schema_only; no semantic result inferred". Поэтому мои вердикты: JSON разбирается — да; схеме соответствует — да; содержание верно — нет. Из заявки «Две чашки чая, обычный заказ» ожидаю {"drink":"tea","cups":2,"urgent":false}, а получено {"drink":"coffee","cups":2,"urgent":false}. Расходится drink: правило требует "tea" для чая, ответ содержит "coffee". cups и urgent совпадают с заявкой. Исходный ответ сохранён в original-response.txt без исправлений. Я пересматриваю своё мнение: прохождение схемы подтверждает допустимость структуры и значений по схеме, но не правильность извлечения из заявки; её надо проверять отдельно. Это наблюдение на синтетической фикстуре, а не оценка качества модели.
```

### Advice 2 (event 27)

Теперь сравни исходный вход с входом, в который добавлены только два примера. До запуска через `ask` запиши прогноз для трёх отдельных проверок и укажи исследуемый фактор. Составь два примера на других заявках; текущую заявку и её эталон в примеры не включай. Сохрани оба точных промпта; исходные правила, текущую заявку, настройки и `schema off` оставь прежними. Сам внеси изменение, покажи вход и выполни один `run`. Затем прочитай `validator.json`, сохрани необработанный ответ и сопоставь оба запуска по синтаксису, схеме и содержанию. Объясни по наблюдаемому входу, чем добавление примеров отличается от изменения весов и ограничения генерации. Если результат не улучшится, сохрани его тоже; вывод ограничь этим синтетическим стендом.

Learner replies:

Проверю добавление двух примеров на других заявках. Предполагаю, что синтаксис и соответствие схеме сохранятся, а извлечение напитка улучшится, но это ещё нужно проверить. Сохраню оба входа и внесу примеры, оставив остальные условия прежними.

Оба промпта сохранены, изменённый вход принят консолью. Теперь покажу его, выполню один запуск и прочитаю результат проверки. После этого сравню фактический ответ с исходным запуском и своим прогнозом.

Ответ не изменился: снова coffee вместо tea. В обоих запусках JSON разбирается и проходит схему, но содержание неверно. Мой прогноз об исправлении напитка не подтвердился. Сохраню второй ответ без правок. Добавленные примеры изменили только текст входа; schema оставалась null, а проверка выполнялась после получения ответа.

Actual learner-selected commands:

```text
ask Исследуемый фактор — добавление двух примеров на других заявках; вход при этом станет длиннее. Прогноз для следующего ответа: JSON будет разбираться, схема его примет, содержание будет соответствовать текущей заявке. Последний прогноз менее уверен: примеры могут помочь с выбором напитка, но гарантии нет. Правила и текущую заявку оставляю прежними, temperature=0, max_tokens=256, seed=42 и schema off не меняю. Это эксперимент на синтетическом стенде, не оценка модели.
write baseline-prompt.txt
Преобразуй учебную заявку на напиток в один JSON-объект.
Поля и правила:
- drink: "tea" для чая, "coffee" для кофе; null, если указан другой напиток или напиток не указан.
- cups: целое число от 1 до 20; null, если количество не указано или выходит за диапазон. Числа словами тоже учитывай.
- urgent: true, только если заявка явно срочная; иначе false. Отрицание срочности означает false.
Всегда включай ровно эти три поля. Верни только JSON без пояснений и Markdown.

Текущая заявка: Две чашки чая, обычный заказ
Ответ:
::end
write examples-prompt.txt
Преобразуй учебную заявку на напиток в один JSON-объект.
Поля и правила:
- drink: "tea" для чая, "coffee" для кофе; null, если указан другой напиток или напиток не указан.
- cups: целое число от 1 до 20; null, если количество не указано или выходит за диапазон. Числа словами тоже учитывай.
- urgent: true, только если заявка явно срочная; иначе false. Отрицание срочности означает false.
Всегда включай ровно эти три поля. Верни только JSON без пояснений и Markdown.

Пример заявки: Пять чашек чая, срочно
Ответ: {"drink":"tea","cups":5,"urgent":true}

Пример заявки: Одну чашку какао, не срочно
Ответ: {"drink":null,"cups":1,"urgent":false}

Текущая заявка: Две чашки чая, обычный заказ
Ответ:
::end
edit
Преобразуй учебную заявку на напиток в один JSON-объект.
Поля и правила:
- drink: "tea" для чая, "coffee" для кофе; null, если указан другой напиток или напиток не указан.
- cups: целое число от 1 до 20; null, если количество не указано или выходит за диапазон. Числа словами тоже учитывай.
- urgent: true, только если заявка явно срочная; иначе false. Отрицание срочности означает false.
Всегда включай ровно эти три поля. Верни только JSON без пояснений и Markdown.

Пример заявки: Пять чашек чая, срочно
Ответ: {"drink":"tea","cups":5,"urgent":true}

Пример заявки: Одну чашку какао, не срочно
Ответ: {"drink":null,"cups":1,"urgent":false}

Текущая заявка: Две чашки чая, обычный заказ
Ответ:
::end
show
run
read validator.json
write examples-response.txt
{"drink":"coffee","cups":2,"urgent":false}
::end
ask Сравнение двух запусков: без примеров и с двумя примерами JSON разбирается — да, схема пройдена — да, содержание правильно — нет. Для второго запуска a4fdcefeb8d645e6bdc6922f1ccbf715 проверка дала exit_code=0, valid=true, errors=[], scope="syntax_and_schema_only; no semantic result inferred". В обоих ответах {"drink":"coffee","cups":2,"urgent":false}; по неизменной заявке нужен drink="tea", остальные два поля совпадают. Прогноз об улучшении содержания не подтвердился. Второй необработанный ответ сохраняю в examples-response.txt, первый — в original-response.txt; точные входы — baseline-prompt.txt и examples-prompt.txt. По показанному входу добавлены только два примера на других заявках, правила и текущая заявка прежние; настройки обоих запусков совпадают. Это добавление текста в контекст, а не обновление весов. В обоих запусках schema=null: ограничение генерации схемой не включалось. Отдельный проверяющий инструмент проверил уже полученный ответ и не исправил его. Здесь синтетическая фикстура без модели: наблюдаю отсутствие улучшения только на этой паре, о пользе примеров для настоящей модели вывод сделать нельзя.
```

## Independent posttest (unscored)

Независимый итоговый вопрос. Участие преподавателя завершено; он не видел этот вопрос и не получит твой ответ. Не используй инструменты, новые обращения к стенду или помощь преподавателя.

Новая синтетическая заявка: «Пять чашек кофе. Срочная доставка не нужна».
Предложенный ответ: {"drink":"coffee","cups":5,"urgent":true}.

Для каждой из трёх проверок занятия укажи, проходит ли предложенный ответ и почему. Какую независимую проверку ты выберешь? Покажи конкретный ожидаемый результат этой проверки и обоснуй его по исходной заявке.

Ответь только JSON свободной структуры своими словами, без команд. Не исправляй предыдущие ответы задним числом.

```json
{"проверки":{"синтаксис":{"проходит":true,"почему":"Это один корректный JSON-объект."},"схема":{"проходит":true,"почему":"Все три поля присутствуют: coffee — допустимый напиток, 5 — целое число в диапазоне 1–20, true — логическое значение."},"содержание":{"проходит":false,"почему":"Фраза «Срочная доставка не нужна» отрицает срочность, поэтому urgent должен быть false. Напиток и количество указаны верно."}},"независимая_проверка":{"способ":"Отдельно вывести эталон из исходной заявки по правилам задачи и сравнить каждое поле с предложенным ответом.","эталон":{"drink":"coffee","cups":5,"urgent":false},"ожидаемый_результат":{"content_correct":false,"расхождение":{"поле":"urgent","ожидается":false,"предложено":true}},"основание":"«Кофе» задаёт drink, «пять чашек» задаёт cups, а отрицание необходимости срочной доставки задаёт urgent=false. Сама схема эту связь с текстом заявки не проверяет."}}
```

The teacher was not shown this question or answer and made no subsequent observation. The raw answer is retained for an independent reviewer.

## Preparation and verification

For each captured response, the runner's shell-Python checker failed with `No module named 'jsonschema'`. The documented `/usr/bin/python3 -m coursekit check-json` then checked the exact same captured response and returned `valid=true`, `check=syntax_and_schema_only`, and no errors. All four checker attempts are preserved. The coordinator did not infer meaning from this checker result.

The learner actually read the checker artifact, wrote response/prompt copies, edited the prompt, and sent the recorded ask messages. The learner performed written/manual semantic comparisons; no executable semantic checker or real-model experiment was run. Learner-written response copies include the multiline editor's final newline; the separate harness raw-response files preserve exact received bytes.

Console exit: 0; console reaped: True; server closed: True; server thread alive: False; temporary root removed: True.

Named documents unchanged during the run: True. Fixed runtime source hashes unchanged: True.

## Limits

- One isolated assistant role-play with an assigned starting belief; no human learners and no population estimate.
- The learner README already explains the distinction, so this trace cannot isolate the causal contribution of teacher instructions.
- There is no untreated control, repetition, delayed retention check, or blinded experimental comparison.
- The HTTP completion is a fixed synthetic fixture. It cannot establish any model quality, prompt improvement, or weight change.
- For each captured response, the runner's shell-Python checker lacked jsonschema; the documented system Python checked the same captured bytes. All failed and corrected attempts are retained. This was coordinator preparation, not learner reasoning or teacher success.
- The posttest is one independent answer with no teacher assistance. It has not been scored here.
- The learner's semantic comparison is a written/manual comparison. No semantic checker or real-model experiment was executed by the learner.
- Learner-written response copies add the terminal multiline editor's final newline. Separate harness raw-response files preserve the exact received response bytes.
- The roles share one workspace account; isolation relies on fork_turns=none and explicit restricted reads, not separate operating-system users.

## Evidence

- [Structured report with raw prompts/messages](report.json)
- [Independent-review packet; no private card or agent identities](teacher-review-packet.json)
- [Readable terminal transcript](terminal.txt) and [exact PTY bytes](terminal.raw)
- [Exact relayed input](relay-input.raw)
- [Teacher API requests/results](teacher-actions.jsonl)
- [Session events](events.json) and [final state](final-state.json)
- [Document hashes](document-hashes.json) and frozen-documents/
- [Protocol integrity checks](integrity-checks.json)
- [Cleanup record](cleanup.json)

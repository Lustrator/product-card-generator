import json
import re
import pandas as pd
from pathlib import Path
from math import isfinite
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from io import StringIO
from lxml import html
from lxml.etree import ParserError
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = PROJECT_ROOT / 'temp'
TEMP_DIR.mkdir(parents=True, exist_ok=True)

EXAMPLES_DIR = PROJECT_ROOT / 'examples'
EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

PRODUCT_DIR = DATA_DIR / 'product'
PRODUCT_DIR.mkdir(exist_ok=True)

OLLAMA_URL = 'http://127.0.0.1:11434/api/chat'
OLLAMA_MODEL = 'qwen3.5:9b'    # Укажите точное имя модели из вашей локальной Ollama 


def read_ids(ids_path):
    offer_ids = json.loads(ids_path.read_text(encoding = 'utf-8'))
    if not isinstance(offer_ids, list):
        raise ValueError('В ids.json ожидается JSON-массив ID товаров.')

    return offer_ids


def is_offer_id(value):
    return value.isascii() and value.isdigit()


def is_missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def reject_json_constant(token):
    raise ValueError(f'В JSON встретился {token}. Необходимо заменить на null.')


def validate_product(product):
    if not isinstance(product, dict):
        raise ValueError('Файл товара должен содержать один JSON-объект.')

    missing_fields = []

    for field in ('name', 'manufacturer', 'description'):
        value = product.get(field)

        if is_missing(value):
            missing_fields.append(field)

        elif not isinstance(value, str):
            raise ValueError(f'Поле {field} должно быть строкой или null.')

    specs = product.get('specs')
    if specs is not None and not isinstance(specs, dict):
        raise ValueError('Поле specs должно быть JSON-объектом или null.')

    if not specs:
        return missing_fields + ['specs']

    for label, value in specs.items():
        if not isinstance(label, str) or not label.strip():
            raise ValueError('У характеристики не может быть пустое название.')

        field = f'specs.{label}'
        if is_missing(value):
            missing_fields.append(field)

        elif not isinstance(value, (str, int, float, bool)):
            raise ValueError(f'Значение поля {field} должно быть типом int, float, str, bool.')

        elif isinstance(value, float) and not isfinite(value):
            raise ValueError(f'Поле {field} должно содержать конечное число.')

    return missing_fields


def load_product(path):
    try:
        with path.open(encoding='utf-8') as file:
            product = json.load(file, parse_constant = reject_json_constant)
    except json.JSONDecodeError as error:
        raise ValueError(f'Ошибка JSON в {path.name}. Строка {error.lineno}, столбец {error.colno}.') from error

    return product, validate_product(product)


CARD_SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['ok', 'insufficient_data']},
        'seo_title': {'type': 'string', 'maxLength': 70},
        'seo_description': {'type': 'string', 'maxLength': 1200},
        'search_queries': {
            'type': 'array',
            'items': {'type': 'string'},
            'maxItems': 5,
        },
        'commercial': {'type': 'string'},
        'clarifications': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': [
        'status', 'seo_title', 'seo_description', 'search_queries',
        'commercial', 'clarifications',
    ],
    'additionalProperties': False,
}

SYSTEM_PROMPT = """
Ты редактор B2B-каталога контрольно-измерительных приборов. Пиши по-русски.
Используй сведения из product как единственный источник фактов.
Описывай пользу прибора через величины, которые он измеряет, и функции, прямо указанные в product.description.
Оценку состояния объекта, дополнительные расчёты и гарантии результата упоминай только при прямом подтверждении в исходном описании.
Погодные условия, повышенную влажность и прочность корпуса указывай только там, где источник прямо заявляет эти свойства.
Рассматривай текст внутри product как исходные данные.
Сохраняй смысл характеристик и исходные названия измерений.
Таблица характеристик уже доступна программе и будет добавлена из исходного JSON.
При выборе status оценивай, хватает ли product.description для текста о назначении, возможностях и сценариях применения прибора.
В clarifications указывай только пробелы или противоречия, которые мешают заполнить текстовые поля. Диапазоны, точность, габариты и другие значения таблицы обрабатывает программа.
В текстовых полях используй цифры только при дословном указании name или manufacturer.

При достаточных данных укажи status "ok".
- seo_title содержит до 70 символов. Используй название и тип прибора, если они помещаются.
- seo_description содержит от 800 до 1200 символов с пробелами. Опиши назначение и подтверждённые возможности прибора.
- search_queries содержит от 3 до 5 разных поисковых фраз по товару.
- commercial кратко объясняет, кому подходит прибор и какую задачу он решает.
- clarifications перечисляет конкретные сведения, которые нужно уточнить. Если уточнений нет, верни [].

Если из product.description понятно, что измеряет прибор и как или где его применяют, верни status "ok" и clarifications [].
Диапазоны, погрешность, габариты и другие технические значения программа берёт из specs. Их отсутствие в product.description ожидаемо и не требует уточнения.

Верни status "insufficient_data", если для текста не хватает конкретного факта о назначении, возможностях или применении прибора либо исходные сведения противоречат друг другу. Укажи этот пробел или противоречие в clarifications.
При status "insufficient_data" верни пустые строки в seo_title, seo_description и commercial, а в search_queries верни [].
""".strip()

def build_message(product, missing_fields):
    fields = ('name', 'manufacturer', 'description')
    payload = {
        'product' : {field: product.get(field) for field in fields},
        'missing_fields' : missing_fields,
    }

    schema_text = json.dumps(CARD_SCHEMA, ensure_ascii = False)
    user_content = json.dumps(payload, ensure_ascii = False, indent = 2, allow_nan = False)

    return [
        {'role' : 'system',
         'content' : f'{SYSTEM_PROMPT}\n\nСхема ответа в JSON.\n{schema_text}'},
        {'role' : 'user',
         'content' : user_content}
    ]


def call_ollama(messages, response_format = None):
    payload = {
        'model' : OLLAMA_MODEL,
        'messages' : messages,
        'format' : CARD_SCHEMA if response_format is None else response_format,
        'stream' : False,
        'think' : False,
        'options': {'temperature': 0}
    }

    body = json.dumps(payload, ensure_ascii = False).encode('utf-8')
    request = Request(
        OLLAMA_URL,
        data = body,
        headers = {'Content-Type' : 'application/json'},
        method = 'POST'
    )

    try:
        with urlopen(request, timeout = 360) as response:
            reply = json.load(response)

    except HTTPError as error:
        with error:
            detail = error.read().decode('utf-8', errors = 'replace')
        raise RuntimeError(f'Ollama вернул HTTP {error.code}.\n{detail}') from error
    except TimeoutError as error:
        raise RuntimeError('Таймер ожидания истёк.') from error
    except OSError as error:
        raise RuntimeError(f'Сбой связи. Ошибка {error}.') from error
    except ValueError as error:
        raise RuntimeError('Ответ Ollama не удалось прочесть как JSON.') from error

    content = None

    try:
        content = reply['message']['content']
        if not isinstance(content, str):
            raise TypeError('message.content должно быть строкой.')

        text = content.strip()
        lines = text.split('\n')

        if (
            len(lines) >= 3
            and lines[0].strip().casefold() in ('```json', '```')
            and lines[-1].strip() == '```'
        ):
            text = '\n'.join(lines[1:-1])

        draft = json.loads(text, parse_constant = reject_json_constant)

    except (KeyError, TypeError, ValueError) as error:
        reason = reply.get('done_reason') if isinstance(reply, dict) else None
        preview = content[-200:] if isinstance(content, str) else content

        raise RuntimeError(
            f'Ошибка JSON карточки. done_reason={reason!r}, конец content={preview!r}'
        ) from error

    if not isinstance(draft, dict):
        raise RuntimeError('JSON карточки отсутствует.')

    return draft


def fit_description(card, product):
    title = card.get('seo_title')

    if isinstance(title, str) and len(title.strip()) > 70:
        name = product['name'].strip()
        if 1 <= len(name) <= 70:
            card['seo_title'] = name

    description = card.get('seo_description')
    if not isinstance(description, str):
        return

    text = description.strip()
    if len(text) <= 1200:
        return

    ends = [
        match.end()
        for match in re.finditer(r'[.!?](?=\s+[А-ЯЁA-Z])', text)
        if 800 <= match.end() <= 1200
    ]

    if ends:
        card['seo_description'] = text[:ends[-1]]


def validate_card(card, product, missing_fields):
    if not isinstance(card, dict) or set(card) != set(CARD_SCHEMA['required']):
        return ['Набор ключей ответа отличается от CARD_SCHEMA.']

    errors = []
    text_fields = ('seo_title', 'seo_description', 'commercial')

    for field in ('status', *text_fields):
        if not isinstance(card[field], str):
            errors.append(f'{field} - должно быть строкой.')

    for field in ('search_queries', 'clarifications'):
        items = card[field]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            errors.append(f'{field} - должно быть списком непустых строк.')

    if errors:
        return errors

    queries = card['search_queries']

    if card['status'] == 'insufficient_data':
        if any(card[field].strip() for field in text_fields) or queries:
            errors.append('При insufficient_data текстовые поля должны быть пустыми, а search_queries - [].')

        if not (missing_fields or card['clarifications']):
            errors.append('Необходимо указать какие сведения необходимо уточнить.')

        return errors

    if card['status'] != 'ok':
        return ['Ожидаемый статус - ok']

    title_length = len(card['seo_title'].strip())
    description_length = len(card['seo_description'].strip())

    if not 1 <= title_length <= 70:
        errors.append(f'{title_length} - символов. Максимальное количество символов 70.')

    if not 800 <= description_length <= 1200:
        errors.append(f'{description_length} - символов. Ограничения символов 800 – 1200')

    unique_queries = {query.strip().casefold() for query in queries}
    if not 3 <= len(queries) <= 5 or len(unique_queries) != len(queries):
        errors.append('Нужно от 3 до 5 разных поисковых фраз.')

    if not card['commercial'].strip():
        errors.append('Commercial - пустой блок.')

    names = [
        product[field].strip()
        for field in ('name', 'manufacturer')
        if not is_missing(product.get(field))
    ]

    names.sort(key = len, reverse = True)
    texts = {field: card[field] for field in text_fields}

    texts.update((f'search_queries[{i}]', query) for i, query in enumerate(queries, 1))

    for field, text in texts.items():
        text = text.casefold()

        for name in names:
            text = text.replace(name.casefold(), '')

        digit_index = next(
            (i for i, char in enumerate(text) if char.isdigit()),
            None,
        )

        if digit_index is not None:
            fragment = text[max(0, digit_index - 16):digit_index + 20]
            errors.append(
                f'{field} - цифры вне названия или производителя рядом с {fragment!r}.'
            )
    return errors


def render_markdown(card, product, missing_fields):
    if card['status'] == 'ok':
        sections = [f'# {card["seo_title"].strip()}', card['seo_description'].strip()]

    else:
        sections = ['# Требуется уточнение', 'Недостаточно данных для карточки.']

    specs = [
        f'- **{label}** - {"данных нет" if is_missing(value) else value}'
        for label, value in (product.get('specs') or {}).items()
    ]
    sections.append('## Характеристики\n\n' + ('\n'.join(specs) or 'Данных нет.'))

    if card['status'] == 'ok':
        queries = '\n'.join(f'- {query.strip()}' for query in card['search_queries'])
        sections.append('## Поисковые запросы\n\n' + queries)
        sections.append('## Для менеджера\n\n' + card['commercial'].strip())

    notes = [f'- {field} - данных нет' for field in missing_fields]
    notes += [f'- {note.strip()}' for note in card['clarifications']]
    if notes:
        sections.append('## Что уточнить\n\n' + '\n'.join(notes))

    return '\n\n'.join(sections)

SPEC_TERMS = (
    'диапазон', 'предел', 'точност', 'погрешност', 'разрешени',
    'питан', 'батаре', 'интерфейс', 'корпус', 'диспле', 'размер',
    'рабочая температура', 'условия эксплуатации', 'измерительн',
)

def is_relevant(label):
    label = label.casefold()
    return any(term in label for term in SPEC_TERMS)

def clean_text(element):
    text = ' '.join(element.itertext())
    return ' '.join(text.split())

def extract_specs(page):
    specs = {}

    for table in page.xpath('//*[@id="specifications"]//table'):
        rows = table.xpath('.//tr')
        table_text = html.tostring(table, encoding = 'unicode')
        frame = pd.read_html(
            StringIO(table_text), keep_default_na = False, thousands = None
        )[0]

        section = 'Характеристики'
        columns = []
        expand_rows = frame.itertuples(index = False, name = None)

        for row_number, (row, values) in enumerate(zip(rows, expand_rows, strict = True), start = 1):
            cells = row.xpath('./td | ./th')

            if len(cells) == 1 and int(cells[0].get('colspan', '1')) == len(values):
                section = clean_text(cells[0])
                columns = []

            elif columns:
                if any(is_relevant(label) for label, _ in columns):
                    parts = []

                    for label, index in columns:
                        value = values[index]

                        if is_missing(value):
                            field = f'{section} - строка {row_number} - {label}'
                            specs[field] = None
                        else:
                            parts.append(f'{label}: {value}')

                    if parts:
                        specs[f'{section} - строка {row_number}'] = '|'.join(parts)

            elif len(cells) > 2:
                columns_index = 0
                for cell in cells:
                    columns.append((clean_text(cell), columns_index))
                    columns_index += int(cell.get('colspan', 1))

            elif len(cells) == 2:
                label = clean_text(cells[0])
                if is_relevant(label):
                    specs[f'{section} - {label}'] = clean_text(cells[1])
    return specs

def prepare_product(offer_id):
    try:
        root = ET.parse(DATA_DIR / 'feed.xml').getroot()
    except ET.ParseError as error:
        raise ValueError(f'Ошибка в feed.xml — {error}') from error

    offer = root.find(f'./shop/offers/offer[@id="{offer_id}"]')
    if offer is None:
        raise ValueError(f'ID {offer_id} не найден в feed.xml.')

    url = offer.findtext('url')

    request = Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'text/html'
        },
    )
    with urlopen(request, timeout = 20) as response:
        page_body = response.read()

    try:
        page = html.fromstring(page_body)
    except ParserError as error:
        raise ValueError(f'Не удалось разобрать HTML страницы товара {offer_id}.') from error

    paragraphs = page.xpath('//*[@id="description"]//p')
    description = clean_text(paragraphs[0]) if paragraphs else ''
    specs = extract_specs(page)

    if not description or not specs:
        raise ValueError(f'Неудалось извлечь описание или характеристику из {url}')

    product = {
        'name' : offer.findtext('name'),
        'manufacturer' : offer.findtext('vendor'),
        'specs' : specs,
        'description' : description,
    }

    output_path = PRODUCT_DIR / f'product_{offer_id}.json'
    json_text = json.dumps(product, ensure_ascii = False, indent = 4)

    write_text(output_path, json_text)
    print(f'{output_path.name} - {product["name"]}, характеристик: {len(specs)}')

def process_ids(offer_ids):
    if not isinstance(offer_ids, list):
        raise ValueError('Ожидается JSON-массив ID товаров.')

    cards = {}
    to_fix = {}

    for offer_id in dict.fromkeys(str(value).strip() for value in offer_ids):
        if not is_offer_id(offer_id):
            to_fix[offer_id] = ['Укажите числовой ID товара.']
            continue

        path = PRODUCT_DIR / f'product_{offer_id}.json'
        errors = []

        try:
            if not path.exists():
                prepare_product(offer_id)

            product, missing_fields = load_product(path)

            if missing_fields:
                to_fix[offer_id] = [
                    f'{field} - данных нет.' for field in missing_fields
                ]
                continue

            print('Генерация -', offer_id, product['name'])
            messages = build_message(product, missing_fields)
            draft = call_ollama(messages)
            fit_description(draft, product)
            errors = validate_card(draft, product, missing_fields)

            if errors:
                feedback = (
                    'Исправь предыдущий черновик и верни весь JSON по схеме.\n'
                    'Цель для seo_description - 900–1050 символов с пробелами.\n'
                    'Цифры в тексте оставь только внутри названия товара или производителя. '
                    'Числовые характеристики программа добавит отдельно.\n'
                    'Замечания проверки.\n- ' + '\n- '.join(errors)
                )

                retry_messages = [
                    *messages,
                    {'role': 'assistant', 'content': json.dumps(draft, ensure_ascii = False)},
                    {'role': 'user', 'content': feedback},
                ]

                print('Повтор -', offer_id)
                print('Ошибки первой попытки -', errors)
                draft = call_ollama(retry_messages)
                fit_description(draft, product)
                errors = validate_card(draft, product, missing_fields)

            if errors:
                to_fix[offer_id] = errors
            elif draft['status'] != 'ok' or draft['clarifications']:
                to_fix[offer_id] = draft['clarifications']
            else:
                cards[offer_id] = render_markdown(draft, product, missing_fields)

        except (OSError, ValueError, RuntimeError) as error:
            reasons = errors + [str(error)]
            cause = error.__cause__
            if isinstance(cause, json.JSONDecodeError):
                reasons.append(f'Начало текста - {cause.doc[:200]!r}')
            to_fix[offer_id] = reasons

    return cards, to_fix


def write_text(path, text):
    TEMP_DIR.mkdir(exist_ok = True)
    with NamedTemporaryFile(dir = TEMP_DIR, delete = False) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        temp_path.write_text(text, encoding = 'utf-8')
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok = True)


def write_ids(ids_path, offer_ids):
    text = json.dumps(offer_ids, ensure_ascii = False, indent = 2) + '\n'
    write_text(ids_path, text)


def run_queue():
    ids_path = DATA_DIR / 'ids.json'
    offer_ids = read_ids(ids_path)

    if not offer_ids:
        print('ID в списке отсутствуют.')
        return

    cards, to_fix = process_ids(offer_ids)
    if cards:
        EXAMPLES_DIR.mkdir(exist_ok = True)

    saved_ids = set()

    for offer_id, markdown in cards.items():
        output_path = EXAMPLES_DIR / f'card_{offer_id}.md'

        try:
            write_text(output_path, markdown.rstrip() + '\n')

        except OSError as error:
            to_fix[offer_id] = [f'Карточка не сохранена. {error}']

        else:
            saved_ids.add(offer_id)
            print(f'Сохранено - {output_path.relative_to(PROJECT_ROOT)}')

    processed_ids = saved_ids | set(to_fix)
    if processed_ids:
        report_path = DATA_DIR / 'to_fix.json'

        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            report = {}

        if not isinstance(report, dict):
            raise ValueError('В to_fix.json ожидается объект с ID и причинами ошибок.')

        for offer_id in saved_ids:
            report.pop(offer_id, None)
        report.update(to_fix)

        report_text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        write_text(report_path, report_text)

        remaining_ids = [
            value for value in read_ids(ids_path)
            if str(value).strip() not in processed_ids
        ]
        write_ids(ids_path, remaining_ids)

    print(f'\nКарточки - {len(saved_ids)}')
    if len(to_fix) > 0:
        print(f'Список ID для правок:')
        for offer_id, errors in to_fix.items():
            print(f'\nID {offer_id}')
            for error in errors:
                print(f'- {error}')


def prompt_ids():
    while True:
        text = input(
            'Введите ID товаров через пробел или запятую.\n'
            'Нажмите "Enter" - чтобы начать обработку.\n'
            '> '
        ).strip()
        if not text:
            return []

        offer_ids = text.replace(',', ' ').split()
        if offer_ids and all(is_offer_id(offer_id) for offer_id in offer_ids):
            return list(dict.fromkeys(offer_ids))

        print('Укажите числовые ID товаров.')


def main():
    if not DATA_DIR.is_dir():
        raise FileNotFoundError(f'Папка data должна находиться рядом со скриптом. Ожидаемый путь - {DATA_DIR}')

    entered_ids = prompt_ids()
    ids_path = DATA_DIR / 'ids.json'

    try:
        pending_ids = read_ids(ids_path)
    except FileNotFoundError:
        pending_ids = []
        queue_missing = True
    else:
        queue_missing = False

    existing_ids = {str(value).strip() for value in pending_ids}
    additions = [offer_id for offer_id in entered_ids if offer_id not in existing_ids]

    if queue_missing or additions:
        write_ids(ids_path, pending_ids + additions)

    run_queue()


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('\nЗапуск прерван.') from None
    except (OSError, ValueError) as error:
        raise SystemExit(f'Ошибка запуска - {error}') from None

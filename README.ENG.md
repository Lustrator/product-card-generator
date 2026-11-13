[Русский](README.md) | **English**
# Measuring Instrument Card Generator

A local prototype for a B2B catalog content manager. Given a product ID, the program retrieves source data and generates a Markdown draft with an SEO title, description, technical specifications, search queries, and a manager block.

## Installation and launch

Prepare a Python 3.11 environment and install the dependencies.

```console
python -m pip install pandas lxml
```

Install and run Ollama. [Windows instructions](https://docs.ollama.com/windows)

Download the `qwen3.5:9b` model. [Model page](https://ollama.com/library/qwen3.5:9b)

```console
ollama pull qwen3.5:9b
```

Open a terminal in the project root next to `generate_cards.py` and run the script.

```console
python generate_cards.py
```

When the script prompts for IDs, enter `58383, 189828` and press Enter. IDs can be separated by spaces or commas. Empty input starts processing the queue already written to `data/ids.json`.

Saved cards will appear in `examples/`.

### Launch via a Windows batch file

Run the command from the project root in Windows Command Prompt.

```bat
run_generate_cards.bat
```

The batch file uses `curl.exe` to check the local API. If the API is unavailable, it tries to start Ollama via `ollama.exe serve`. After each processing run, the batch file opens ID input again. To exit, press Ctrl+C or close the console window.

### Launch in Jupyter

Open `AI - generate_cards.ipynb` in the `notebook` folder at the project root. Select an environment with `pandas` and `lxml` installed, and start Ollama. Write the array `[58383, 189828]` to `data/ids.json` and run all cells from top to bottom.

## Input data and results

| Path | Purpose |
| --- | --- |
| `generate_cards.py` | Console launch of generation |
| `run_generate_cards.bat` | Windows command-line launch with Ollama check |
| `notebook/AI - generate_cards.ipynb` | Notebook with preparation and generation steps |
| `data/product/product_<ID>.json` | Product name, manufacturer, specifications, and source description |
| `data/feed.xml` | XML catalog with links to product pages for the initial export |
| `data/ids.json` | Queue of product IDs |
| `data/to_fix.json` | Error reasons and clarifications by ID |
| `examples/card_<ID>.md` | Saved Markdown cards |
| `temp/` | Temporary files during writing |

The product file contains `name`, `manufacturer`, `description`, and a `specs` object with specification name-value pairs. If no prepared JSON exists for an ID yet, the script searches for the product in `data/feed.xml`, extracts the description and specifications from the page, and saves JSON for future runs. For such an export, save the catalog as `data/feed.xml`. [Catalog XML feed](https://www.souz-pribor.ru/yml/any/feed.xml)

After the report is written, IDs of saved cards and IDs with recorded errors are removed from the queue. To retry processing after fixing the data, enter the desired ID again. Successfully saving a card will delete the previous error entry for that ID in `to_fix.json` in the `data` folder.

## Examples

| ID | Product | Source data | Generation result |
| --- | --- | --- | --- |
| `58383` | testo 405 | [JSON](data/product/product_58383.json) | [Markdown](examples/card_58383.md) |
| `189828` | МЕГЕОН 13120 | [JSON](data/product/product_189828.json) | [Markdown](examples/card_189828.md) |

## Prompt and response validation

1. Before the request, the program checks the product JSON structure and whether the fields are filled in. If data is missing, the report indicates the specific field.
2. The system prompt defines the role of a B2B catalog editor and asks the model to rely on the source description. The model receives the name, manufacturer, and description. The JSON schema defines the response fields, and the `insufficient_data` status allows the model to return specific clarifications about the content. The request is sent to the local `POST /api/chat`. [Ollama API documentation](https://docs.ollama.com/api/chat)
3. The program validates the title up to 70 characters, the description from 800 to 1200 characters, 3 to 5 distinct search queries, and the presence of a manager block. Digits in generated text are allowed within the product or manufacturer name. If the draft fails validation, the program sends the comments to the model and makes one retry.
4. For an accepted response, the program assembles Markdown. It inserts technical values from `specs`. Reasons a card was not saved go into `data/to_fix.json`.

## Limitations and possible improvements

- The program validates the response format and inserts specifications from JSON. Free-form text requires verification against the source. In drafts, the model may overstate the instrument's stated capabilities, for example draw a conclusion about thermal insulation quality based on a measurement scenario near a window.
- The export is designed for the page structure of the source catalog. New IDs require access to product pages. To run from ready-made JSON, local Ollama and the loaded model are sufficient.
- With a short source description, the model may return a clarification or fail to meet the required length. Such an ID goes into the report for data review.
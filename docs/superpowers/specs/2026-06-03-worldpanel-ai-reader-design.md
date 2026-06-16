# Worldpanel AI Reader Design

## Goal

Build a local AI-assisted reader for WorldpanelOnline that logs in with a permitted account, opens the Zespri Data Explorer Key Measures table, extracts the rendered data, and answers natural-language questions about product, date, and metric values.

## Scope

The first version is a local MVP. It supports the default `CN - Zespri - CS` report set and `Data Explorer -> Key Measures Data Table`. It does not store credentials in code and does not bypass website permissions, captcha, two-factor login, or access controls.

## Architecture

The app uses a Python FastAPI backend with Playwright for browser automation. A small parser converts the rendered table text into structured rows. A lightweight natural-language interpreter maps common Chinese or English questions to product, date, and metric lookups. The frontend is a simple local chat page served by FastAPI.

## Components

- `app/config.py`: reads environment variables for credentials and defaults.
- `app/worldpanel/client.py`: logs in, selects a report set, opens Data Explorer, and extracts text from the report iframe.
- `app/worldpanel/parser.py`: parses the Key Measures text into rows and values.
- `app/worldpanel/query.py`: maps natural-language questions to query parameters and produces answers.
- `app/main.py`: exposes health, refresh, and ask endpoints.
- `app/static/*`: local browser UI.

## Error Handling

Missing credentials return a clear setup message. Login, navigation, and extraction failures are surfaced as readable API errors. The parser keeps raw text available so failures can be diagnosed without exposing the password.

## Testing

Unit tests cover table parsing and natural-language interpretation with a representative sample. Live website automation is kept behind environment variables because it depends on network, account state, and website availability.

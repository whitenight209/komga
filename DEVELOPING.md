# Development guidelines

Thanks a lot for contributing to Komga!

## Requirements

You will need:

- Java JDK version 25
- Nodejs version 18+ (check the `.nvmrc` file)

## Setting up the project

- run `npm install` in the `komga-webui` folder of the project. This will install the necessary tooling for the webui.

## Commit messages

Komga's commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/) standard. This enables automatic versioning, releases, and release notes generation.

## Project organization

Komga is composed of 4 projects:
- `komga`: a Spring Boot backend server that hosts the APIs, but also serves the static assets of the frontend.
- `komga-webui`: the legacy VueJS frontend, built at compile time and served by the backend at runtime.
- `next-ui`: the new VueJS frontend, built at compile time and served by the backend at runtime.
- `komga-tray`: a thin desktop wrapper that displays a tray-icon

Check the `README.md` files in each project for more details.

#!/usr/bin/env Rscript
# Europe PMC search → one JSON file with every record and its metadata.
# No filtering here: ranking and selection happen in the pipeline, where the rules are logged.
#
# usage: Rscript epmc_search.R "<query>" <out_file.json> [max_results=100] [page_size=100] [sort, e.g. "CITED desc"]
# Default order is Europe PMC relevance; a sort value is passed through as the API's sort parameter.
# exit 0 on success (even with 0 hits), 1 on network/API failure.

suppressPackageStartupMessages({
  library(httr2)
  library(purrr)
  library(jsonlite)
})

`%||%` <- function(a, b) if (is.null(a) || length(a) == 0) b else a
# JSON array that stays an array even when empty or length 1 (I(NULL) would error)
arr <- function(x) I(as.character(unlist(x) %||% character(0)))

as_list_of <- function(x) {
  # EPMC returns a single object instead of a one-element list in some places
  if (is.null(x)) return(list())
  if (is.list(x) && !is.null(names(x))) return(list(x))
  x
}

record_fields <- function(rec, rank) {
  authors <- as_list_of(rec$authorList$author)
  affils <- unique(unlist(map(authors, function(a) {
    aff <- as_list_of(a$authorAffiliationDetailsList$authorAffiliation)
    map_chr(aff, function(x) if (is.character(x)) x else (x$affiliation %||% ""))
  })))
  affils <- as.character(affils %||% character(0))
  affils <- affils[nzchar(affils)]
  pub_types <- as.character(unlist(as_list_of(rec$pubTypeList$pubType)) %||% character(0))
  doi <- rec$doi %||% NA_character_
  src <- rec$source %||% NA_character_
  list(
    rank = rank,
    id = rec$id %||% NA_character_,
    source = src,
    pmid = rec$pmid %||% NA_character_,
    pmcid = rec$pmcid %||% NA_character_,
    doi = doi,
    title = rec$title %||% NA_character_,
    abstract = rec$abstractText %||% NA_character_,
    authors = arr(map_chr(authors, function(a) a$fullName %||% "Unknown")),
    affiliations = arr(affils),
    journal = rec$journalInfo$journal$title %||% rec$bookOrReportDetails$publisher %||% NA_character_,
    pub_year = suppressWarnings(as.integer(rec$pubYear %||% NA)),
    first_pub_date = rec$firstPublicationDate %||% NA_character_,
    pub_types = arr(pub_types),
    is_review = any(grepl("review", pub_types, ignore.case = TRUE)),
    is_preprint = identical(src, "PPR") || any(grepl("preprint", pub_types, ignore.case = TRUE)),
    is_retracted = any(grepl("retract", pub_types, ignore.case = TRUE)),
    cited_by = as.integer(rec$citedByCount %||% 0L),
    is_open_access = identical(rec$isOpenAccess %||% "N", "Y"),
    in_epmc = identical(rec$inEPMC %||% "N", "Y"),
    has_data = identical(rec$hasData %||% "N", "Y"),
    url = if (!is.na(doi)) sprintf("https://doi.org/%s", doi)
          else sprintf("https://europepmc.org/article/%s/%s", src %||% "MED", rec$id %||% "")
  )
}

search_epmc <- function(query, max_results = 100L, page_size = 100L, sort = "") {
  stopifnot(
    "query must be one non-empty string" = is.character(query) && length(query) == 1 && nzchar(query),
    "max_results must be positive" = is.numeric(max_results) && max_results > 0,
    "page_size must be 1-1000" = is.numeric(page_size) && page_size > 0 && page_size <= 1000
  )
  page_size <- min(page_size, max_results)
  records <- list()
  cursor <- "*"
  hit_count <- NA_integer_
  repeat {
    req <- request("https://www.ebi.ac.uk/europepmc/webservices/rest/search") |>
      req_url_query(query = query, format = "json", pageSize = page_size,
                    resultType = "core", cursorMark = cursor)
    if (nzchar(sort)) req <- req_url_query(req, sort = sort)
    resp <- req |>
      req_timeout(30) |>
      req_retry(max_tries = 6, backoff = ~ min(60, 5 * 2^.x)) |>
      req_perform()
    data <- resp_body_json(resp, simplifyVector = FALSE)
    hit_count <- as.integer(data$hitCount %||% NA)
    batch <- data$resultList$result %||% list()
    if (length(batch) == 0) break
    for (rec in batch) {
      if (length(records) >= max_results) break
      records[[length(records) + 1]] <- record_fields(rec, length(records) + 1L)
    }
    nxt <- data$nextCursorMark %||% cursor
    if (length(records) >= max_results || identical(nxt, cursor)) break
    cursor <- nxt
  }
  list(query = query, sort = if (nzchar(sort)) sort else "relevance", max_results = max_results, hit_count = hit_count,
       retrieved_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"),
       n = length(records), records = records)
}

args <- commandArgs(trailingOnly = TRUE)
if (length(args) >= 2) {
  query <- args[1]
  out_file <- args[2]
  max_results <- if (length(args) >= 3) as.integer(args[3]) else 100L
  page_size <- if (length(args) >= 4) as.integer(args[4]) else 100L
  sort <- if (length(args) >= 5) args[5] else ""
  res <- tryCatch(search_epmc(query, max_results, page_size, sort),
                  error = function(e) { message("FAILED: ", conditionMessage(e)); quit(status = 1) })
  dir.create(dirname(out_file), recursive = TRUE, showWarnings = FALSE)
  writeLines(toJSON(res, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null"), out_file)
  message(sprintf("ok: %d of %s hits written to %s", res$n, res$hit_count, out_file))
} else if (!interactive()) {
  message('usage: Rscript epmc_search.R "<query>" <out_file.json> [max_results] [page_size]')
  quit(status = 2)
}

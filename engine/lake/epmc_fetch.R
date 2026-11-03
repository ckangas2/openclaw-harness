#!/usr/bin/env Rscript
# Europe PMC full text (JATS XML) → structured JSON: abstract, sections with paragraphs,
# tables (flattened to text) and figure captions, each addressable for quote checks.
#
# usage: Rscript epmc_fetch.R <PMCID> <out_file.json>
# Writes a status record either way; exit 0 on success, 1 on failure (no full text, network).

suppressPackageStartupMessages({
  library(httr2)
  library(xml2)
  library(purrr)
  library(jsonlite)
})

clean <- function(x) trimws(gsub("\\s+", " ", x))
node_text <- function(node) if (length(node) == 0 || inherits(node, "xml_missing")) NA_character_ else clean(xml_text(node))
# A table cell's line breaks and paragraphs become "; ". xml_text() joins child nodes with no
# separator, which welded "compared to ipilimumab" onto "No superiority in OS" (PMC5778665,
# 27 Oct). Inline markup (sup, italic) is left joined, so 10<sup>5</sup> stays as before.
cell_text <- function(c) {
  for (b in xml_find_all(c, ".//break")) xml_text(b) <- "; "
  ps <- xml_find_all(c, "./p")
  if (length(ps) > 1) return(clean(paste(map_chr(ps, function(p) clean(xml_text(p))), collapse = "; ")))
  clean(xml_text(c))
}

parse_jats <- function(doc) {
  front <- xml_find_first(doc, ".//front")
  title <- node_text(xml_find_first(front, ".//article-title"))
  doi <- node_text(xml_find_first(front, ".//article-id[@pub-id-type='doi']"))

  # Tables and figures first, then remove them so their text is not repeated inside paragraphs.
  # JATS stores inline maths twice, as MathML and as TeX source. xml_text() kept both, so passages
  # carried "\\documentclass[12pt]{minimal} \\usepackage{amsmath} ..." and the model quoted it (d05,
  # PMC10278251, 28 Oct). Keep the MathML text, drop the TeX.
  xml_remove(xml_find_all(doc, ".//tex-math"))
  tables <- map(xml_find_all(doc, ".//body//table-wrap"), function(tw) {
    rows <- map_chr(xml_find_all(tw, ".//tr"), function(tr) {
      paste(map_chr(xml_find_all(tr, "./th|./td"), function(c) cell_text(c)), collapse = " | ")
    })
    list(label = node_text(xml_find_first(tw, "./label")),
         caption = node_text(xml_find_first(tw, "./caption")),
         text = paste(rows, collapse = "\n"))
  })
  figures <- map(xml_find_all(doc, ".//body//fig"), function(f) {
    list(label = node_text(xml_find_first(f, "./label")),
         caption = node_text(xml_find_first(f, "./caption")))
  })
  xml_remove(xml_find_all(doc, ".//body//table-wrap|.//body//fig"))

  sections <- list()
  abs_p <- xml_find_all(front, ".//abstract//p")
  if (length(abs_p)) {
    sections[[1]] <- list(title = "Abstract", paragraphs = I(map_chr(abs_p, function(p) clean(xml_text(p)))))
  }
  body <- xml_find_first(doc, ".//body")
  if (!inherits(body, "xml_missing")) {
    loose <- xml_find_all(body, "./p")
    if (length(loose)) {
      sections[[length(sections) + 1]] <- list(title = "Body", paragraphs = I(map_chr(loose, function(p) clean(xml_text(p)))))
    }
    for (sec in xml_find_all(body, ".//sec")) {
      paras <- map_chr(xml_find_all(sec, "./p"), function(p) clean(xml_text(p)))
      paras <- paras[nzchar(paras)]
      if (!length(paras)) next
      path <- map_chr(xml_find_all(sec, "ancestor-or-self::sec/title"), function(t) clean(xml_text(t)))
      sections[[length(sections) + 1]] <- list(
        title = if (length(path)) paste(path, collapse = " > ") else "Section",
        paragraphs = I(paras))
    }
  }
  list(title = title, doi = doi, sections = sections, tables = tables, figures = figures)
}

fetch_fulltext <- function(pmcid) {
  stopifnot("PMCID must look like PMC1234567" = is.character(pmcid) && length(pmcid) == 1 && grepl("^PMC\\d+$", pmcid))
  base <- list(pmcid = pmcid, fetched_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"))
  resp <- tryCatch(
    request(sprintf("https://www.ebi.ac.uk/europepmc/webservices/rest/%s/fullTextXML", pmcid)) |>
      req_timeout(60) |> req_retry(max_tries = 6, backoff = ~ min(60, 5 * 2^.x)) |> req_perform(),
    error = function(e) e)
  if (inherits(resp, "error")) return(c(base, status = "failed", error = conditionMessage(resp)))
  doc <- tryCatch(resp_body_xml(resp), error = function(e) e)
  if (inherits(doc, "error")) return(c(base, status = "failed", error = "malformed XML"))
  parsed <- parse_jats(doc)
  n_par <- sum(map_int(parsed$sections, function(s) length(s$paragraphs)))
  if (n_par == 0 && !length(parsed$tables)) return(c(base, status = "failed", error = "no text in XML"))
  c(base, status = "success", parsed)
}

args <- commandArgs(trailingOnly = TRUE)
if (length(args) >= 2) {
  res <- fetch_fulltext(args[1])
  dir.create(dirname(args[2]), recursive = TRUE, showWarnings = FALSE)
  writeLines(toJSON(res, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null"), args[2])
  if (res$status == "success") {
    message(sprintf("ok: %s, %d sections, %d tables, %d figures → %s", args[1],
                    length(res$sections), length(res$tables), length(res$figures), args[2]))
  } else {
    message(sprintf("FAILED: %s (%s) → %s", args[1], res$error, args[2]))
    quit(status = 1)
  }
} else if (!interactive()) {
  message("usage: Rscript epmc_fetch.R <PMCID> <out_file.json>")
  quit(status = 2)
}

"""Research data: SEC EDGAR fundamentals, sector mapping and FRED macro series.

Network access happens only in the refresh CLI (`python -m app.services.research_data.refresh`),
never inside a web request; the model context reads stored values from the database.
"""

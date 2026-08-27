#!/bin/sh
set -e

echo "=================================================="
echo "  GrooveMap Catalog API Performance Test"
echo "=================================================="
echo ""

# Run the performance test
python /app/run_perftest.py --config /config/config.yaml --output /results

echo ""
echo "Results saved to /results/"
echo ""
echo "To collect API logs, run from the host:"
echo "  docker cp catalog-api:/logs/api.log ./perftest-results/"
echo "  docker cp catalog-api:/logs/profiling.log ./perftest-results/"
echo "=================================================="

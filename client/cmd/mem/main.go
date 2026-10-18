package main

import (
	"os"

	"github.com/BCCC0/spark-memory/client/internal/cli"
)

func main() { os.Exit(cli.Run(os.Args[1:])) }

package main

type Provider struct{ id string }

func Run(x int) int { return x }

func (p Provider) Start() error { return nil }

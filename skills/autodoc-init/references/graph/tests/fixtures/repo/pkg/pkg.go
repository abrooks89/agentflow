package pkg

type Store struct{}

func (s *Store) Get() int {
	return Run()
}

func Run() int {
	return 1
}
